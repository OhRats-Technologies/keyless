/*
 * Native click-through multi-monitor Wayland gaze reticle overlay for Keyless.
 *
 * Uses wlr-layer-shell-v1 (supported by KWin Plasma 6 Wayland, Hyprland, Sway)
 * and xdg-output-unstable-v1 to place one transparent, click-through overlay
 * surface on every physical output, reading the 32-byte seqlock shared memory
 * channel (/keyless_gaze_v1) directly at 60 Hz with minimal dirty-rect damage.
 *
 * Build: gcc -O2 overlay_wayland.c -o keyless-overlay -lwayland-client -lrt -lm
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>

#define MAX_OUTPUTS 8
#define FLAG_FACE_DETECTED (1u << 0)
#define FLAG_BLINKING           (1u << 1)
#define FLAG_DEV_MONITOR_BORDER (1u << 4)

/* Packed 32-byte seqlock layout matching keyless/ipc/state.py ("=QddIIQ") */
struct __attribute__((packed)) gaze_shm_layout {
    uint64_t seq;
    double x;
    double y;
    uint32_t flags;
    uint32_t reserved;
    uint64_t timestamp_ns;
};

/* --- Minimal embedded Wayland protocol interfaces (wlr-layer-shell & xdg-output) --- */

struct zwlr_layer_shell_v1;
struct zwlr_layer_surface_v1;
struct zxdg_output_manager_v1;
struct zxdg_output_v1;

static const struct wl_interface zwlr_layer_surface_v1_interface;
static const struct wl_interface zxdg_output_v1_interface;

static const struct wl_interface *layer_shell_get_types[] = {
    &zwlr_layer_surface_v1_interface, &wl_surface_interface, &wl_output_interface, NULL, NULL
};
static const struct wl_message layer_shell_requests[] = {
    { "get_layer_surface", "no?ous", layer_shell_get_types },
    { "destroy", "", (const struct wl_interface *[]){} },
};
static const struct wl_interface zwlr_layer_shell_v1_interface = {
    "zwlr_layer_shell_v1", 4, 2, layer_shell_requests, 0, NULL
};

static const struct wl_interface *null_types_4[] = { NULL, NULL, NULL, NULL };
static const struct wl_message layer_surface_requests[] = {
    { "set_size", "uu", null_types_4 },
    { "set_anchor", "u", null_types_4 },
    { "set_exclusive_zone", "i", null_types_4 },
    { "set_margin", "iiii", null_types_4 },
    { "set_keyboard_interactivity", "u", null_types_4 },
    { "get_popup", "o", null_types_4 },
    { "ack_configure", "u", null_types_4 },
    { "destroy", "", null_types_4 },
};
static const struct wl_message layer_surface_events[] = {
    { "configure", "uuu", null_types_4 },
    { "closed", "", null_types_4 },
};
static const struct wl_interface zwlr_layer_surface_v1_interface = {
    "zwlr_layer_surface_v1", 4, 8, layer_surface_requests, 2, layer_surface_events
};

static const struct wl_interface *xdg_out_get_types[] = {
    &zxdg_output_v1_interface, &wl_output_interface
};
static const struct wl_message xdg_out_mgr_requests[] = {
    { "destroy", "", null_types_4 },
    { "get_xdg_output", "no", xdg_out_get_types },
};
static const struct wl_interface zxdg_output_manager_v1_interface = {
    "zxdg_output_manager_v1", 3, 2, xdg_out_mgr_requests, 0, NULL
};

static const struct wl_message xdg_out_requests[] = {
    { "destroy", "", null_types_4 },
};
static const struct wl_message xdg_out_events[] = {
    { "logical_position", "ii", null_types_4 },
    { "logical_size", "ii", null_types_4 },
    { "done", "", null_types_4 },
    { "name", "s", null_types_4 },
    { "description", "s", null_types_4 },
};
static const struct wl_interface zxdg_output_v1_interface = {
    "zxdg_output_v1", 3, 1, xdg_out_requests, 5, xdg_out_events
};

struct zwlr_layer_surface_v1_listener {
    void (*configure)(void *data, struct zwlr_layer_surface_v1 *s, uint32_t serial, uint32_t w, uint32_t h);
    void (*closed)(void *data, struct zwlr_layer_surface_v1 *s);
};

struct zxdg_output_v1_listener {
    void (*logical_position)(void *data, struct zxdg_output_v1 *o, int32_t x, int32_t y);
    void (*logical_size)(void *data, struct zxdg_output_v1 *o, int32_t w, int32_t h);
    void (*done)(void *data, struct zxdg_output_v1 *o);
    void (*name)(void *data, struct zxdg_output_v1 *o, const char *name);
    void (*description)(void *data, struct zxdg_output_v1 *o, const char *desc);
};

/* --- Overlay State --- */

struct output_overlay {
    struct wl_output *wl_out;
    struct zxdg_output_v1 *xdg_out;
    struct wl_surface *surface;
    struct zwlr_layer_surface_v1 *layer_surface;
    struct wl_buffer *buffer;
    uint32_t *pixels;
    size_t shm_size;
    int32_t log_x, log_y;
    int32_t log_w, log_h;
    int32_t buf_w, buf_h;
    bool configured;
    bool has_drawn;
    bool has_border;
    int32_t prev_x, prev_y, prev_r;
};

static struct wl_display *g_display = NULL;
static struct wl_compositor *g_compositor = NULL;
static struct wl_shm *g_shm = NULL;
static struct zwlr_layer_shell_v1 *g_layer_shell = NULL;
static struct zxdg_output_manager_v1 *g_xdg_output_mgr = NULL;
static struct output_overlay g_outputs[MAX_OUTPUTS];
static int g_output_count = 0;
static volatile sig_atomic_t g_running = 1;

static void handle_sig(int sig) { (void)sig; g_running = 0; }

static int create_shm_file(size_t size) {
    char name[64];
    snprintf(name, sizeof(name), "/keyless-wl-ov-%d-%ld", (int)getpid(), (long)clock());
    int fd = shm_open(name, O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return -1;
    shm_unlink(name);
    if (ftruncate(fd, (off_t)size) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static bool alloc_output_buffer(struct output_overlay *ov, int32_t w, int32_t h) {
    if (w <= 0 || h <= 0) return false;
    if (ov->buffer && ov->buf_w == w && ov->buf_h == h) return true;
    if (ov->buffer) {
        wl_buffer_destroy(ov->buffer);
        ov->buffer = NULL;
    }
    if (ov->pixels) {
        munmap(ov->pixels, ov->shm_size);
        ov->pixels = NULL;
    }
    size_t stride = (size_t)w * 4;
    size_t size = stride * (size_t)h;
    int fd = create_shm_file(size);
    if (fd < 0) return false;
    uint32_t *data = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (data == MAP_FAILED) {
        close(fd);
        return false;
    }
    memset(data, 0, size);
    struct wl_shm_pool *pool = wl_shm_create_pool(g_shm, fd, (int32_t)size);
    ov->buffer = wl_shm_pool_create_buffer(pool, 0, w, h, (int32_t)stride, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool);
    close(fd);
    ov->pixels = data;
    ov->shm_size = size;
    ov->buf_w = w;
    ov->buf_h = h;
    return true;
}

static void layer_configure(void *data, struct zwlr_layer_surface_v1 *s, uint32_t serial, uint32_t w, uint32_t h) {
    struct output_overlay *ov = (struct output_overlay *)data;
    wl_proxy_marshal((struct wl_proxy *)s, 6, serial); /* ack_configure */
    if (w > 0 && h > 0) {
        if (ov->log_w <= 0) ov->log_w = (int32_t)w;
        if (ov->log_h <= 0) ov->log_h = (int32_t)h;
        if (alloc_output_buffer(ov, (int32_t)w, (int32_t)h)) {
            wl_surface_attach(ov->surface, ov->buffer, 0, 0);
            wl_surface_damage_buffer(ov->surface, 0, 0, (int32_t)w, (int32_t)h);
            wl_surface_commit(ov->surface);
            ov->configured = true;
        }
    }
}

static void layer_closed(void *data, struct zwlr_layer_surface_v1 *s) {
    (void)data; (void)s;
    g_running = 0;
}

static const struct zwlr_layer_surface_v1_listener g_layer_listener = {
    .configure = layer_configure,
    .closed = layer_closed,
};

static void xdg_out_pos(void *data, struct zxdg_output_v1 *o, int32_t x, int32_t y) {
    (void)o;
    struct output_overlay *ov = (struct output_overlay *)data;
    ov->log_x = x;
    ov->log_y = y;
}
static void xdg_out_size(void *data, struct zxdg_output_v1 *o, int32_t w, int32_t h) {
    (void)o;
    struct output_overlay *ov = (struct output_overlay *)data;
    ov->log_w = w;
    ov->log_h = h;
}
static void xdg_out_done(void *d, struct zxdg_output_v1 *o) { (void)d; (void)o; }
static void xdg_out_name(void *d, struct zxdg_output_v1 *o, const char *n) { (void)d; (void)o; (void)n; }
static void xdg_out_desc(void *d, struct zxdg_output_v1 *o, const char *s) { (void)d; (void)o; (void)s; }

static const struct zxdg_output_v1_listener g_xdg_out_listener = {
    .logical_position = xdg_out_pos,
    .logical_size = xdg_out_size,
    .done = xdg_out_done,
    .name = xdg_out_name,
    .description = xdg_out_desc,
};

static void reg_global(void *data, struct wl_registry *reg, uint32_t id, const char *iface, uint32_t ver) {
    (void)data;
    if (strcmp(iface, wl_compositor_interface.name) == 0) {
        g_compositor = wl_registry_bind(reg, id, &wl_compositor_interface, ver < 4 ? ver : 4);
    } else if (strcmp(iface, wl_shm_interface.name) == 0) {
        g_shm = wl_registry_bind(reg, id, &wl_shm_interface, 1);
    } else if (strcmp(iface, zwlr_layer_shell_v1_interface.name) == 0) {
        g_layer_shell = (struct zwlr_layer_shell_v1 *)wl_registry_bind(reg, id, &zwlr_layer_shell_v1_interface, ver < 4 ? ver : 4);
    } else if (strcmp(iface, zxdg_output_manager_v1_interface.name) == 0) {
        g_xdg_output_mgr = (struct zxdg_output_manager_v1 *)wl_registry_bind(reg, id, &zxdg_output_manager_v1_interface, ver < 3 ? ver : 3);
    } else if (strcmp(iface, wl_output_interface.name) == 0 && g_output_count < MAX_OUTPUTS) {
        g_outputs[g_output_count++].wl_out = wl_registry_bind(reg, id, &wl_output_interface, ver < 3 ? ver : 3);
    }
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t id) { (void)d; (void)r; (void)id; }
static const struct wl_registry_listener g_reg_listener = { reg_global, reg_remove };

/* Premultiplied ARGB8888 blend */
static inline void blend_px(uint32_t *dst, uint8_t r, uint8_t g, uint8_t b, float alpha) {
    if (alpha <= 0.004f) return;
    if (alpha > 1.0f) alpha = 1.0f;
    uint32_t a = (uint32_t)(alpha * 255.0f + 0.5f);
    uint32_t pr = (uint32_t)(r * alpha + 0.5f);
    uint32_t pg = (uint32_t)(g * alpha + 0.5f);
    uint32_t pb = (uint32_t)(b * alpha + 0.5f);
    uint32_t cur = *dst;
    uint32_t ca = (cur >> 24) & 0xffu;
    if (a >= ca) {
        *dst = (a << 24) | (pr << 16) | (pg << 8) | pb;
    }
}

static void clear_rect(struct output_overlay *ov, int32_t cx, int32_t cy, int32_t pad) {
    int32_t x0 = cx - pad; if (x0 < 0) x0 = 0;
    int32_t y0 = cy - pad; if (y0 < 0) y0 = 0;
    int32_t x1 = cx + pad; if (x1 >= ov->buf_w) x1 = ov->buf_w - 1;
    int32_t y1 = cy + pad; if (y1 >= ov->buf_h) y1 = ov->buf_h - 1;
    for (int32_t y = y0; y <= y1; ++y) {
        memset(&ov->pixels[y * ov->buf_w + x0], 0, (size_t)(x1 - x0 + 1) * 4);
    }
    wl_surface_damage_buffer(ov->surface, x0, y0, x1 - x0 + 1, y1 - y0 + 1);
}


/* Developer debug: 4px OhRats positive green (#43b777) perimeter border on active monitor */
static void paint_border_edges(struct output_overlay *ov, uint32_t color) {
    const int32_t t = 4;
    if (ov->buf_w < t * 2 || ov->buf_h < t * 2) return;
    /* Top and bottom strips */
    for (int32_t y = 0; y < t; ++y) {
        uint32_t *row_top = &ov->pixels[y * ov->buf_w];
        uint32_t *row_bot = &ov->pixels[(ov->buf_h - 1 - y) * ov->buf_w];
        for (int32_t x = 0; x < ov->buf_w; ++x) {
            row_top[x] = color;
            row_bot[x] = color;
        }
    }
    /* Left and right strips */
    for (int32_t y = t; y < ov->buf_h - t; ++y) {
        uint32_t *row = &ov->pixels[y * ov->buf_w];
        for (int32_t x = 0; x < t; ++x) {
            row[x] = color;
            row[ov->buf_w - 1 - x] = color;
        }
    }
    wl_surface_damage_buffer(ov->surface, 0, 0, ov->buf_w, t);
    wl_surface_damage_buffer(ov->surface, 0, ov->buf_h - t, ov->buf_w, t);
    wl_surface_damage_buffer(ov->surface, 0, t, t, ov->buf_h - 2 * t);
    wl_surface_damage_buffer(ov->surface, ov->buf_w - t, t, t, ov->buf_h - 2 * t);
}

static void draw_active_border(struct output_overlay *ov) {
    /* Premultiplied ARGB8888 for #43b777 at ~86% alpha (a=220, r=58, g=158, b=103) */
    const uint32_t green = (220u << 24) | (58u << 16) | (158u << 8) | 103u;
    paint_border_edges(ov, green);
}

static void clear_active_border(struct output_overlay *ov) {
    paint_border_edges(ov, 0u);
}

static void draw_reticle(struct output_overlay *ov, int32_t cx, int32_t cy, bool blinking) {
    /*
     * Calm visionOS-inspired rounded focus ring (OhRats terracotta-amber #e27a3b + ivory pip).
     * Avoids sharp crosshair edges and never expands during blinks (dims softly instead)
     * so peripheral motion never triggers involuntary cursor-chasing saccades.
     */
    const float radius = 14.0f;
    const float dim = blinking ? 0.34f : 1.0f;
    const int32_t pad = 26;
    int32_t x0 = cx - pad; if (x0 < 0) x0 = 0;
    int32_t y0 = cy - pad; if (y0 < 0) y0 = 0;
    int32_t x1 = cx + pad; if (x1 >= ov->buf_w) x1 = ov->buf_w - 1;
    int32_t y1 = cy + pad; if (y1 >= ov->buf_h) y1 = ov->buf_h - 1;

    for (int32_t y = y0; y <= y1; ++y) {
        float dy = (float)(y - cy);
        for (int32_t x = x0; x <= x1; ++x) {
            float dx = (float)(x - cx);
            float dist = sqrtf(dx * dx + dy * dy);
            uint32_t *px = &ov->pixels[y * ov->buf_w + x];

            /* Soft translucent inner fill */
            if (dist < radius - 1.2f) {
                blend_px(px, 226, 122, 59, 0.08f * dim);
            }
            /* Anti-aliased rounded focus ring */
            float d = fabsf(dist - radius);
            if (d < 2.2f) {
                float ring_a = d < 1.1f ? 0.82f : 0.82f * (1.0f - (d - 1.1f) / 1.1f);
                blend_px(px, 226, 122, 59, ring_a * dim);
            }
            /* Subtle center anchor pip */
            if (dist <= 2.6f) {
                float a = dist <= 1.6f ? 0.88f : 0.88f * (2.6f - dist);
                blend_px(px, 244, 243, 223, a * dim);
            }
        }
    }
    wl_surface_damage_buffer(ov->surface, x0, y0, x1 - x0 + 1, y1 - y0 + 1);
}

static bool read_gaze_shm(const volatile struct gaze_shm_layout *shm, double *x, double *y, uint32_t *flags) {
    for (int i = 0; i < 6; ++i) {
        uint64_t s1 = __atomic_load_n(&shm->seq, __ATOMIC_ACQUIRE);
        if (s1 & 1u) continue;
        double lx = shm->x;
        double ly = shm->y;
        uint32_t lf = shm->flags;
        uint64_t s2 = __atomic_load_n(&shm->seq, __ATOMIC_ACQUIRE);
        if (s1 == s2) {
            *x = lx; *y = ly; *flags = lf;
            return s1 > 0;
        }
    }
    return false;
}

/* ===================================================================== */
/* Calibration stage ("follow the light")                                */
/* ===================================================================== */
/*
 * Stage mode: `keyless-overlay <gaze_shm> <pid> <stage_shm>`.
 * Dims every output like a theatre and renders one hypnotic stimulus that
 * travels across displays. Python owns all timing (positions are labels), so
 * this loop only draws the latest committed state.
 *
 * Stimulus anatomy (outside -> in):
 *   halo   - soft amber gaussian glow: peripheral saliency, findable anywhere;
 *   spiral - 3-arm sqrt-compressed spiral rotating inward: a motion-energy
 *            "pull" toward the centre that holds pursuit without effort;
 *   pip    - crisp ivory point with a dark ring: the exact label location.
 * During holds Python shrinks `size`; the contracting spiral funnels attention
 * onto the pip. `burst` draws an expanding ring as confirmation.
 */
#define STAGE_ACTIVE (1u << 0)
#define STAGE_TARGET (1u << 1)
#define STAGE_PAUSED (1u << 2)
#define STAGE_DONE   (1u << 3)

struct __attribute__((packed)) stage_shm_layout {
    uint64_t seq;
    double x;
    double y;
    float size;
    float phase;
    float dim;
    float burst;
    uint32_t flags;
    uint32_t reserved;
};

static bool read_stage_shm(const volatile struct stage_shm_layout *shm, struct stage_shm_layout *out) {
    for (int i = 0; i < 8; ++i) {
        uint64_t s1 = __atomic_load_n(&shm->seq, __ATOMIC_ACQUIRE);
        if (s1 & 1u) continue;
        struct stage_shm_layout tmp;
        tmp.x = shm->x; tmp.y = shm->y; tmp.size = shm->size; tmp.phase = shm->phase;
        tmp.dim = shm->dim; tmp.burst = shm->burst; tmp.flags = shm->flags;
        uint64_t s2 = __atomic_load_n(&shm->seq, __ATOMIC_ACQUIRE);
        if (s1 == s2) { tmp.seq = s1; *out = tmp; return s1 > 0; }
    }
    return false;
}

static inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
static inline float smooth(float e0, float e1, float x) {
    float t = clampf((x - e0) / (e1 - e0), 0.0f, 1.0f);
    return t * t * (3.0f - 2.0f * t);
}

/* Premultiplied source-over: dst = src + dst * (1 - src_a). Colours 0..1. */
static inline void over_px(uint32_t *dst, float r, float g, float b, float a) {
    if (a <= 0.002f) return;
    if (a > 1.0f) a = 1.0f;
    uint32_t c = *dst;
    float da = (float)((c >> 24) & 0xffu), dr = (float)((c >> 16) & 0xffu);
    float dg = (float)((c >> 8) & 0xffu), db = (float)(c & 0xffu);
    float k = 1.0f - a;
    uint32_t oa = (uint32_t)clampf(a * 255.0f + da * k, 0.0f, 255.0f);
    uint32_t orr = (uint32_t)clampf(r * a * 255.0f + dr * k, 0.0f, 255.0f);
    uint32_t og = (uint32_t)clampf(g * a * 255.0f + dg * k, 0.0f, 255.0f);
    uint32_t ob = (uint32_t)clampf(b * a * 255.0f + db * k, 0.0f, 255.0f);
    *dst = (oa << 24) | (orr << 16) | (og << 8) | ob;
}

static inline uint32_t dim_color(float dim) {
    uint32_t a = (uint32_t)(clampf(dim, 0.0f, 1.0f) * 0.90f * 255.0f + 0.5f);
    /* Premultiplied near-black with a warm tint (#0b0806). */
    uint32_t r = (a * 11u) / 255u, g = (a * 8u) / 255u, b = (a * 6u) / 255u;
    return (a << 24) | (r << 16) | (g << 8) | b;
}

static void fill_rect(struct output_overlay *ov, int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint32_t c) {
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > ov->buf_w) x1 = ov->buf_w;
    if (y1 > ov->buf_h) y1 = ov->buf_h;
    if (x0 >= x1 || y0 >= y1) return;
    for (int32_t y = y0; y < y1; ++y) {
        uint32_t *row = &ov->pixels[y * ov->buf_w];
        for (int32_t x = x0; x < x1; ++x) row[x] = c;
    }
    wl_surface_damage_buffer(ov->surface, x0, y0, x1 - x0, y1 - y0);
}

static void draw_stimulus(struct output_overlay *ov, float cx, float cy, float R,
                          float phase, float burst, bool paused, int32_t *bx0, int32_t *by0,
                          int32_t *bx1, int32_t *by1) {
    const float reach = R * 2.4f + 6.0f;
    int32_t x0 = (int32_t)floorf(cx - reach), y0 = (int32_t)floorf(cy - reach);
    int32_t x1 = (int32_t)ceilf(cx + reach), y1 = (int32_t)ceilf(cy + reach);
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > ov->buf_w) x1 = ov->buf_w;
    if (y1 > ov->buf_h) y1 = ov->buf_h;
    *bx0 = x0; *by0 = y0; *bx1 = x1; *by1 = y1;
    if (x0 >= x1 || y0 >= y1) return;

    const float master = paused ? 0.38f : 1.0f;
    const float spin = paused ? 0.0f : phase;
    /* Palette: OhRats amber, ivory, deep terracotta. */
    const float ar = 0.886f, ag = 0.478f, ab = 0.231f;
    const float ir = 0.957f, ig = 0.953f, ib = 0.875f;
    const float tr = 0.42f, tg = 0.16f, tb = 0.07f;
    const float breathe = 1.0f + 0.04f * sinf(phase * 2.1f);
    const float Rb = R * breathe;
    const float pip = fmaxf(2.4f, R * 0.045f);

    for (int32_t y = y0; y < y1; ++y) {
        float dy = (float)y + 0.5f - cy;
        uint32_t *row = &ov->pixels[y * ov->buf_w];
        for (int32_t x = x0; x < x1; ++x) {
            float dx = (float)x + 0.5f - cx;
            float d = sqrtf(dx * dx + dy * dy);
            if (d > reach) continue;
            uint32_t *px = &row[x];
            float rho = d / Rb;

            /* 1. halo */
            float halo = expf(-rho * rho * 1.6f) * 0.42f;
            over_px(px, ar, ag, ab, halo * master);

            /* 2. spiral disc */
            if (rho < 1.05f) {
                float th = atan2f(dy, dx);
                float s = sinf(3.0f * th + 17.0f * sqrtf(rho) - 5.5f * spin);
                float band = smooth(-0.25f, 0.25f, s);
                float edge = smooth(1.03f, 0.80f, rho);
                float core = 1.0f - rho;
                float br = ar + (ir - ar) * core * core, bg = ag + (ig - ag) * core * core, bb = ab + (ib - ab) * core * core;
                over_px(px, tr, tg, tb, 0.55f * edge * master);
                over_px(px, br, bg, bb, band * edge * 0.92f * master);
            }

            /* 3. burst ring */
            if (burst > 0.0f && burst < 1.0f) {
                float rr = Rb * (1.0f + 1.3f * burst);
                float w = 2.0f + 3.0f * (1.0f - burst);
                float ring = smooth(w, 0.0f, fabsf(d - rr)) * (1.0f - burst) * (1.0f - burst);
                over_px(px, ir, ig, ib, ring * 0.9f);
            }

            /* 4. pip: dark ring then ivory point (always full strength) */
            if (d < pip + 2.6f) {
                float ringa = smooth(pip + 2.6f, pip + 1.2f, d);
                over_px(px, 0.05f, 0.03f, 0.02f, 0.85f * ringa);
                float dot = smooth(pip + 0.6f, pip - 0.6f, d);
                over_px(px, ir, ig, ib, dot);
            }
        }
    }
    wl_surface_damage_buffer(ov->surface, x0, y0, x1 - x0, y1 - y0);
}

static int stage_loop(const char *stage_name, pid_t watch_pid) {
    char path[128];
    if (stage_name[0] == '/') snprintf(path, sizeof(path), "%s", stage_name);
    else snprintf(path, sizeof(path), "/%s", stage_name);
    int fd = -1;
    for (int tries = 0; tries < 40 && g_running; ++tries) {
        fd = shm_open(path, O_RDONLY, 0);
        if (fd >= 0) break;
        usleep(25000);
    }
    if (fd < 0) { fprintf(stderr, "keyless-stage: no stage shm %s\n", path); return 3; }
    void *mapped = mmap(NULL, sizeof(struct stage_shm_layout), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (mapped == MAP_FAILED) return 3;
    const volatile struct stage_shm_layout *shm = (const volatile struct stage_shm_layout *)mapped;

    float applied_dim[MAX_OUTPUTS];
    int32_t prev_box[MAX_OUTPUTS][4];
    for (int i = 0; i < MAX_OUTPUTS; ++i) { applied_dim[i] = -1.0f; prev_box[i][0] = -1; }

    int wl_fd = wl_display_get_fd(g_display);
    unsigned int tick = 0;
    uint64_t last_seq = 0;
    while (g_running) {
        if (watch_pid > 1 && (++tick % 30u) == 0u) {
            if (kill(watch_pid, 0) != 0 && errno == ESRCH) break;
        }
        while (wl_display_prepare_read(g_display) != 0) wl_display_dispatch_pending(g_display);
        wl_display_flush(g_display);
        struct pollfd pfd = { .fd = wl_fd, .events = POLLIN };
        if (poll(&pfd, 1, 8) > 0) {
            wl_display_read_events(g_display);
            wl_display_dispatch_pending(g_display);
        } else {
            wl_display_cancel_read(g_display);
        }

        struct stage_shm_layout st;
        if (!read_stage_shm(shm, &st)) continue;
        if (st.flags & STAGE_DONE) break;
        if (st.seq == last_seq) continue;
        last_seq = st.seq;
        float dim = (st.flags & STAGE_ACTIVE) ? clampf(st.dim, 0.0f, 1.0f) : 0.0f;
        float qdim = floorf(dim * 64.0f + 0.5f) / 64.0f;
        uint32_t bg = dim_color(qdim);

        for (int i = 0; i < g_output_count; ++i) {
            struct output_overlay *ov = &g_outputs[i];
            if (!ov->configured || !ov->pixels) continue;
            bool dirty = false;
            if (qdim != applied_dim[i]) {
                fill_rect(ov, 0, 0, ov->buf_w, ov->buf_h, bg);
                applied_dim[i] = qdim;
                prev_box[i][0] = -1;
                dirty = true;
            } else if (prev_box[i][0] >= 0) {
                fill_rect(ov, prev_box[i][0], prev_box[i][1], prev_box[i][2], prev_box[i][3], bg);
                prev_box[i][0] = -1;
                dirty = true;
            }
            bool on_this = (st.flags & STAGE_TARGET) &&
                st.x >= (double)ov->log_x && st.x < (double)(ov->log_x + ov->log_w) &&
                st.y >= (double)ov->log_y && st.y < (double)(ov->log_y + ov->log_h);
            if (on_this && st.size > 0.01f) {
                double sx = (double)ov->buf_w / (double)(ov->log_w > 0 ? ov->log_w : ov->buf_w);
                double sy = (double)ov->buf_h / (double)(ov->log_h > 0 ? ov->log_h : ov->buf_h);
                float lx = (float)((st.x - (double)ov->log_x) * sx);
                float ly = (float)((st.y - (double)ov->log_y) * sy);
                float R = 120.0f * st.size * (float)sx;
                int32_t b0, b1, b2, b3;
                draw_stimulus(ov, lx, ly, R, st.phase, st.burst, (st.flags & STAGE_PAUSED) != 0, &b0, &b1, &b2, &b3);
                prev_box[i][0] = b0; prev_box[i][1] = b1; prev_box[i][2] = b2; prev_box[i][3] = b3;
                dirty = true;
            }
            if (dirty) {
                wl_surface_attach(ov->surface, ov->buffer, 0, 0);
                wl_surface_commit(ov->surface);
            }
        }
        wl_display_flush(g_display);
    }
    munmap((void *)shm, sizeof(struct stage_shm_layout));
    return 0;
}

int main(int argc, char **argv) {
    const char *shm_name = (argc > 1 && argv[1][0]) ? argv[1] : "keyless_gaze_v1";
    pid_t watch_pid = (argc > 2) ? (pid_t)atoi(argv[2]) : 0;
    char shm_path[128];
    if (shm_name[0] == '/') snprintf(shm_path, sizeof(shm_path), "%s", shm_name);
    else snprintf(shm_path, sizeof(shm_path), "/%s", shm_name);

    signal(SIGINT, handle_sig);
    signal(SIGTERM, handle_sig);

    g_display = wl_display_connect(NULL);
    if (!g_display) {
        fprintf(stderr, "keyless-overlay: failed to connect to Wayland display\n");
        return 1;
    }

    struct wl_registry *reg = wl_display_get_registry(g_display);
    wl_registry_add_listener(reg, &g_reg_listener, NULL);
    wl_display_roundtrip(g_display);

    if (!g_compositor || !g_shm || !g_layer_shell || g_output_count == 0) {
        fprintf(stderr, "keyless-overlay: missing compositor/shm/wlr-layer-shell/outputs\n");
        wl_display_disconnect(g_display);
        return 2;
    }

    for (int i = 0; i < g_output_count; ++i) {
        struct output_overlay *ov = &g_outputs[i];
        if (g_xdg_output_mgr) {
            ov->xdg_out = (struct zxdg_output_v1 *)wl_proxy_marshal_constructor(
                (struct wl_proxy *)g_xdg_output_mgr, 1, &zxdg_output_v1_interface, NULL, ov->wl_out
            );
            wl_proxy_add_listener((struct wl_proxy *)ov->xdg_out, (void (**)(void))&g_xdg_out_listener, ov);
        }
    }
    wl_display_roundtrip(g_display);

    /* Create one click-through overlay surface per output */
    struct wl_region *empty_region = wl_compositor_create_region(g_compositor);
    for (int i = 0; i < g_output_count; ++i) {
        struct output_overlay *ov = &g_outputs[i];
        ov->surface = wl_compositor_create_surface(g_compositor);
        wl_surface_set_input_region(ov->surface, empty_region);

        /* layer = 3 (ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY) */
        ov->layer_surface = (struct zwlr_layer_surface_v1 *)wl_proxy_marshal_constructor(
            (struct wl_proxy *)g_layer_shell, 0, &zwlr_layer_surface_v1_interface,
            NULL, ov->surface, ov->wl_out, 3u, "keyless-gaze-overlay"
        );
        wl_proxy_add_listener((struct wl_proxy *)ov->layer_surface, (void (**)(void))&g_layer_listener, ov);
        wl_proxy_marshal((struct wl_proxy *)ov->layer_surface, 0, 0u, 0u);   /* set_size(0,0) -> full anchored */
        wl_proxy_marshal((struct wl_proxy *)ov->layer_surface, 1, 15u);      /* anchor ALL edges */
        wl_proxy_marshal((struct wl_proxy *)ov->layer_surface, 2, -1);       /* exclusive_zone = -1 */
        wl_proxy_marshal((struct wl_proxy *)ov->layer_surface, 4, 0u);       /* keyboard_interactivity = NONE */
        wl_surface_commit(ov->surface);
    }
    wl_region_destroy(empty_region);
    wl_display_roundtrip(g_display);

    if (argc > 3 && argv[3][0]) {
        int rc = stage_loop(argv[3], watch_pid);
        for (int i = 0; i < g_output_count; ++i) {
            struct output_overlay *ov = &g_outputs[i];
            if (ov->buffer) wl_buffer_destroy(ov->buffer);
            if (ov->pixels) munmap(ov->pixels, ov->shm_size);
            if (ov->layer_surface) wl_proxy_marshal((struct wl_proxy *)ov->layer_surface, 7);
            if (ov->surface) wl_surface_destroy(ov->surface);
        }
        wl_display_disconnect(g_display);
        return rc;
    }

    /* Open shared gaze memory (retry briefly if writer is still initializing) */
    int shm_fd = -1;
    for (int tries = 0; tries < 20 && g_running; ++tries) {
        shm_fd = shm_open(shm_path, O_RDONLY, 0);
        if (shm_fd >= 0) break;
        usleep(50000);
    }
    const volatile struct gaze_shm_layout *shm = NULL;
    if (shm_fd >= 0) {
        void *mapped = mmap(NULL, sizeof(struct gaze_shm_layout), PROT_READ, MAP_SHARED, shm_fd, 0);
        if (mapped != MAP_FAILED) shm = (const volatile struct gaze_shm_layout *)mapped;
        close(shm_fd);
    }

    int wl_fd = wl_display_get_fd(g_display);
    unsigned int tick = 0;
    while (g_running) {
        if (watch_pid > 1 && (++tick % 30u) == 0u) {
            if (kill(watch_pid, 0) != 0 && errno == ESRCH) break;
        }
        while (wl_display_prepare_read(g_display) != 0) {
            wl_display_dispatch_pending(g_display);
        }
        wl_display_flush(g_display);
        struct pollfd pfd = { .fd = wl_fd, .events = POLLIN };
        if (poll(&pfd, 1, 16) > 0) {
            wl_display_read_events(g_display);
            wl_display_dispatch_pending(g_display);
        } else {
            wl_display_cancel_read(g_display);
        }

        double gx = 0.0, gy = 0.0;
        uint32_t flags = 0;
        bool valid = shm && read_gaze_shm(shm, &gx, &gy, &flags) && (flags & FLAG_FACE_DETECTED);
        bool blinking = (flags & FLAG_BLINKING) != 0;
        bool want_border_global = (flags & FLAG_DEV_MONITOR_BORDER) != 0;

        for (int i = 0; i < g_output_count; ++i) {
            struct output_overlay *ov = &g_outputs[i];
            if (!ov->configured || !ov->pixels) continue;

            bool on_this = valid &&
                gx >= (double)ov->log_x && gx < (double)(ov->log_x + ov->log_w) &&
                gy >= (double)ov->log_y && gy < (double)(ov->log_y + ov->log_h);
            bool want_border = on_this && want_border_global;

            bool dirty = false;
            if (ov->has_drawn) {
                clear_rect(ov, ov->prev_x, ov->prev_y, ov->prev_r);
                ov->has_drawn = false;
                dirty = true;
            }
            if (ov->has_border && !want_border) {
                clear_active_border(ov);
                ov->has_border = false;
                dirty = true;
            }
            if (on_this) {
                double sx = (double)ov->buf_w / (double)(ov->log_w > 0 ? ov->log_w : ov->buf_w);
                double sy = (double)ov->buf_h / (double)(ov->log_h > 0 ? ov->log_h : ov->buf_h);
                int32_t lx = (int32_t)((gx - (double)ov->log_x) * sx);
                int32_t ly = (int32_t)((gy - (double)ov->log_y) * sy);
                draw_reticle(ov, lx, ly, blinking);
                ov->prev_x = lx;
                ov->prev_y = ly;
                ov->prev_r = 36;
                ov->has_drawn = true;
                dirty = true;
            }
            if (want_border) {
                draw_active_border(ov);
                ov->has_border = true;
                dirty = true;
            }
            if (dirty) {
                wl_surface_attach(ov->surface, ov->buffer, 0, 0);
                wl_surface_commit(ov->surface);
            }
        }
        wl_display_flush(g_display);
    }

    if (shm) munmap((void *)shm, sizeof(struct gaze_shm_layout));
    for (int i = 0; i < g_output_count; ++i) {
        struct output_overlay *ov = &g_outputs[i];
        if (ov->buffer) wl_buffer_destroy(ov->buffer);
        if (ov->pixels) munmap(ov->pixels, ov->shm_size);
        if (ov->layer_surface) wl_proxy_marshal((struct wl_proxy *)ov->layer_surface, 7);
        if (ov->surface) wl_surface_destroy(ov->surface);
    }
    wl_display_disconnect(g_display);
    return 0;
}
