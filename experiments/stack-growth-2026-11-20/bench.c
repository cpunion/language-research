#ifndef _GNU_SOURCE
#define _GNU_SOURCE 1
#endif
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach/mach.h>
#endif

enum { COPY, VM_PROTECT, VM_LAZY };
enum { FRAME_BYTES = 512, COPY_MIN = 2048, RESERVE = 1024 * 1024 };

struct frame {
    struct frame *previous;
    uint64_t id;
    unsigned char payload[FRAME_BYTES - sizeof(struct frame *) - sizeof(uint64_t)];
};
_Static_assert(sizeof(struct frame) == FRAME_BYTES, "unexpected frame layout");

struct stack {
    unsigned char *end;
    unsigned char *small_end;
    size_t used;
    size_t capacity;
};

static int mode;
static size_t page_size, initial;
static uint64_t copied_bytes, repaired_links, grows, trims, maps, grows_os, trims_os, unmaps;

static void fail(const char *operation) {
    perror(operation);
    exit(1);
}

static void require(bool ok, const char *message) {
    if (!ok) {
        fprintf(stderr, "%s\n", message);
        exit(1);
    }
}

static void *allocate(size_t size) {
    void *p = malloc(size);
    if (!p) fail("malloc");
    return p;
}

static uint64_t now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("clock_gettime");
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}

static size_t rss(void) {
#ifdef __APPLE__
    mach_task_basic_info_data_t info;
    mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
    require(task_info(mach_task_self(), MACH_TASK_BASIC_INFO, (task_info_t)&info,
                      &count) == KERN_SUCCESS, "task_info memory failed");
    return (size_t)info.resident_size;
#else
    FILE *f = fopen("/proc/self/statm", "r");
    if (!f) fail("open statm");
    size_t total, resident;
    require(fscanf(f, "%zu %zu", &total, &resident) == 2, "read statm failed");
    fclose(f);
    return resident * page_size;
#endif
}

static uint64_t faults(void) {
#ifdef __APPLE__
    task_events_info_data_t info;
    mach_msg_type_number_t count = TASK_EVENTS_INFO_COUNT;
    require(task_info(mach_task_self(), TASK_EVENTS_INFO, (task_info_t)&info,
                      &count) == KERN_SUCCESS, "task_info events failed");
    return (uint64_t)info.faults;
#else
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage)) fail("getrusage");
    return (uint64_t)usage.ru_minflt + (uint64_t)usage.ru_majflt;
#endif
}

static struct frame *frame_at(const struct stack *s, size_t depth) {
    return (struct frame *)(s->end - depth * FRAME_BYTES);
}

static unsigned char *map_range(void *address, size_t size, int protection, int flags) {
    void *p = mmap(address, size, protection, MAP_PRIVATE | MAP_ANONYMOUS | flags, -1, 0);
    if (p == MAP_FAILED) fail("mmap");
    return p;
}

static void discard_tail(struct stack *s, size_t capacity) {
    unsigned char *start = s->end - s->capacity;
    size_t size = s->capacity - capacity;
#ifdef __linux__
    if (mode == VM_LAZY) {
        if (madvise(start, size, MADV_DONTNEED)) fail("madvise DONTNEED");
        return;
    }
#endif
    int protection = mode == VM_PROTECT ? PROT_NONE : PROT_READ | PROT_WRITE;
    map_range(start, size, protection, MAP_FIXED);
}

static void resize(struct stack *s, size_t capacity) {
    require(capacity >= s->used && capacity <= RESERVE, "capacity outside stack budget");
    if (capacity == s->capacity) return;
    if (capacity > s->capacity) ++grows;
    else ++trims;

    if (mode == COPY) {
        unsigned char *old_base = s->end - s->capacity;
        unsigned char *new_end = capacity == COPY_MIN ? s->small_end
                                                      : (unsigned char *)allocate(capacity) + capacity;
        uintptr_t old_end_address = (uintptr_t)s->end;
        memcpy(new_end - s->used, s->end - s->used, s->used);
        copied_bytes += s->used;
        s->end = new_end;
        for (size_t depth = 1; depth <= s->used / FRAME_BYTES; ++depth) {
            struct frame *f = frame_at(s, depth);
            if (f->previous != NULL) {
                size_t offset = old_end_address - (uintptr_t)f->previous;
                f->previous = (struct frame *)(new_end - offset);
                ++repaired_links;
            }
        }
        if (s->capacity != COPY_MIN) free(old_base);
    } else if (capacity > s->capacity) {
        if (mode == VM_PROTECT) {
            if (mprotect(s->end - capacity, capacity - s->capacity, PROT_READ | PROT_WRITE))
                fail("mprotect grow");
            ++grows_os;
        }
    } else {
        discard_tail(s, capacity);
        ++trims_os;
    }
    s->capacity = capacity;
}

static void fill_to(struct stack *s, size_t depth, uint64_t seed) {
    for (size_t i = s->used / FRAME_BYTES + 1; i <= depth; ++i) {
        if (s->capacity < i * FRAME_BYTES) resize(s, s->capacity * 2);
        struct frame *f = frame_at(s, i);
        f->previous = i == 1 ? NULL : frame_at(s, i - 1);
        f->id = seed + i;
        memset(f->payload, (unsigned char)f->id, sizeof(f->payload));
        s->used += FRAME_BYTES;
    }
}

static uint64_t check_stack(const struct stack *s, uint64_t seed) {
    size_t depth = s->used / FRAME_BYTES;
    const struct frame *f = frame_at(s, depth);
    uint64_t sum = 0;
    for (size_t i = depth; i; --i) {
        require(f == frame_at(s, i), "wrong frame link after resize");
        require(f->id == seed + i, "wrong frame id after resize");
        require(f->payload[0] == (unsigned char)f->id &&
                f->payload[sizeof(f->payload) - 1] == (unsigned char)f->id,
                "wrong frame payload after resize");
        sum += f->id;
        f = f->previous;
    }
    require(f == NULL, "wrong stack terminator");
    return sum;
}

int main(int argc, char **argv) {
    require(argc == 3, "usage: bench MODE shallow|deep|cycle-keep|cycle-trim");
    bool no_huge = !strcmp(argv[1], "vm-lazy-nohuge");
    if (!strcmp(argv[1], "copy")) mode = COPY;
    else if (!strcmp(argv[1], "vm-protect")) mode = VM_PROTECT;
    else if (!strcmp(argv[1], "vm-lazy") || no_huge) mode = VM_LAZY;
    else require(false, "unknown mode");
    bool shallow = !strcmp(argv[2], "shallow");
    bool deep = !strcmp(argv[2], "deep");
    bool trim = !strcmp(argv[2], "cycle-trim");
    require(shallow || deep || trim || !strcmp(argv[2], "cycle-keep"), "unknown workload");
    require(!no_huge || shallow, "vm-lazy-nohuge is a shallow-stack control");
    size_t count = shallow ? 16384 : 256;
    size_t depth = shallow ? 2 : 512;
    size_t waves = shallow || deep ? 1 : 16;
    long page = sysconf(_SC_PAGESIZE);
    require(page > 0 && RESERVE % (size_t)page == 0 && (page & (page - 1)) == 0,
            "unsupported page size");
    page_size = (size_t)page;
    initial = mode == COPY ? COPY_MIN : page_size;
    require(initial <= RESERVE, "initial stack exceeds reservation");

    size_t baseline_rss = rss();
    uint64_t start = now();
    struct stack *stacks = allocate(count * sizeof(*stacks));
    unsigned char *reservation = NULL;
    unsigned char *small_pool = NULL;
    if (mode != COPY) {
        int protection = mode == VM_LAZY ? PROT_READ | PROT_WRITE : PROT_NONE;
        reservation = map_range(NULL, RESERVE * count, protection, 0);
        ++maps;
        if (no_huge) {
#if defined(__linux__) && defined(MADV_NOHUGEPAGE)
            if (madvise(reservation, RESERVE * count, MADV_NOHUGEPAGE)) fail("madvise NOHUGEPAGE");
#else
            require(false, "vm-lazy-nohuge requires Linux MADV_NOHUGEPAGE");
#endif
        }
    } else {
        small_pool = allocate(COPY_MIN * count);
    }
    for (size_t i = 0; i < count; ++i) {
        unsigned char *end = mode == COPY ? small_pool + COPY_MIN * (i + 1)
                                          : reservation + RESERVE * (i + 1);
        if (mode == VM_PROTECT) {
            if (mprotect(end - initial, initial, PROT_READ | PROT_WRITE))
                fail("mprotect initial");
            ++grows_os;
        }
        stacks[i] = (struct stack){end, mode == COPY ? end : NULL, 0, initial};
    }
    uint64_t setup_ns = now() - start;
    size_t setup_rss = rss(), full_rss = setup_rss, after_rewind_rss = setup_rss;
    uint64_t checksum = 0, work_ns = 0, trim_ns = 0, wave_ns[16] = {0};
    uint64_t first_fault = faults();
    for (size_t wave = 0; wave < waves; ++wave) {
        start = now();
        for (size_t i = 0; i < count; ++i) fill_to(&stacks[i], depth, i * 1024);
        for (size_t i = 0; i < count; ++i) checksum += check_stack(&stacks[i], i * 1024);
        uint64_t fill_ns = now() - start;
        work_ns += fill_ns;
        size_t full = rss();
        if (full > full_rss) full_rss = full;
        if (waves > 1) {
            start = now();
            for (size_t i = 0; i < count; ++i) {
                stacks[i].used = 2 * FRAME_BYTES;
                if (trim) resize(&stacks[i], initial);
                checksum += check_stack(&stacks[i], i * 1024);
            }
            wave_ns[wave] = now() - start;
            trim_ns += wave_ns[wave];
        }
        wave_ns[wave] += fill_ns;
        after_rewind_rss = rss();
    }
    uint64_t work_faults = faults() - first_fault;
    start = now();
    if (mode == COPY) {
        for (size_t i = 0; i < count; ++i)
            if (stacks[i].capacity != COPY_MIN) free(stacks[i].end - stacks[i].capacity);
        free(small_pool);
    } else {
        if (munmap(reservation, RESERVE * count)) fail("munmap");
        ++unmaps;
    }
    free(stacks);
    uint64_t teardown_ns = now() - start;

    printf("{\"page_bytes\":%zu,\"stacks\":%zu,\"depth\":%zu,\"waves\":%zu,"
           "\"setup_ns\":%" PRIu64 ",\"fill_and_check_ns\":%" PRIu64
           ",\"rewind_and_check_ns\":%" PRIu64 ",\"teardown_ns\":%" PRIu64
           ",\"baseline_rss\":%zu,\"setup_rss\":%zu,\"full_rss\":%zu,\"after_rewind_rss\":%zu,"
           "\"work_faults\":%" PRIu64 ",\"grows\":%" PRIu64 ",\"trims\":%" PRIu64
           ",\"copied_bytes\":%" PRIu64 ",\"repaired_links\":%" PRIu64
           ",\"reservation_bytes\":%zu,\"reserve_calls\":%" PRIu64
           ",\"mprotect_calls\":%" PRIu64 ",\"trim_os_calls\":%" PRIu64
           ",\"unmap_calls\":%" PRIu64 ",\"checksum\":%" PRIu64 ",\"wave_ns\":[",
           page_size, count, depth, waves, setup_ns, work_ns, trim_ns, teardown_ns,
           baseline_rss, setup_rss, full_rss, after_rewind_rss, work_faults, grows, trims,
           copied_bytes, repaired_links, mode == COPY ? (size_t)0 : RESERVE * count,
           maps, grows_os, trims_os, unmaps, checksum);
    for (size_t w = 0; w < waves; ++w) printf("%s%" PRIu64, w ? "," : "", wave_ns[w]);
    puts("]}");
    return 0;
}
