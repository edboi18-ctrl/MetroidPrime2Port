"""The Material Editor's 3D preview: an OpenGL view (PyOpenGL in a pyopengltk frame) of a model or an
area (preview.Scene), each material drawn as the game's PBR stage would draw it with your settings.

The shading follows Aurora's port_surface_pbr (extern/aurora/lib/gx/shader.cpp): the same normal-map
decode and cotangent frame, GGX highlights capped by the light's strength, the environment BRDF
approximation, the occlusion, roughness and metal scales, and the PBR strength mix towards the stage's
own lit texture. What differs: the lighting is a studio light (ambient plus one key light you can
turn), not the room's lights and lightmaps, and the live reflection shows a studio around the model
(the game's probe shows the room). The painted reflection of an environment-mapped material is the
material's own map, sphere-mapped.

Mouse: drag to turn, right- or middle-drag to move, wheel to zoom, Shift-drag to turn the light,
click to pick a material, double-click to turn around the point clicked. Keys (click the view
first): W/A/S/D/Q/E to fly, F to frame the selection, Home to frame all.
"""

import math
import struct
import sys
import time

PROBLEM = None
try:
    from OpenGL import GL
    import pyopengltk
    if sys.platform.startswith("win32"):
        import pyopengltk.win32 as _win32
        _win32.pfd.cDepthBits = 24  # the frame asks for 16; areas need more
    elif sys.platform.startswith("linux"):
        import pyopengltk.linux as _linux
        from OpenGL import GLX as _GLX
        if _GLX.GLX_DEPTH_SIZE not in _linux.fbatt[::2]:
            _linux.fbatt[-1:-1] = [_GLX.GLX_DEPTH_SIZE, 24]
    from pyopengltk import OpenGLFrame
except Exception as _e:  # noqa: BLE001 - ImportError, or the GL library failing to load
    PROBLEM = "The 3D preview needs PyOpenGL and pyopengltk (%s)." % _e
    OpenGLFrame = object

import tkinter as tk

import preview

FOV = math.radians(50.0)

VERTEX_SHADER = """
#version 120
attribute vec3 a_pos;
attribute vec3 a_nrm;
attribute vec2 a_uv;
uniform mat4 u_mv;
uniform mat4 u_proj;
uniform mat3 u_nmat;
varying vec3 v_pos;
varying vec3 v_nrm;
varying vec2 v_uv;
void main() {
    vec4 p = u_mv * vec4(a_pos, 1.0);
    v_pos = p.xyz;
    v_nrm = u_nmat * a_nrm;
    v_uv = a_uv;
    gl_Position = u_proj * p;
}
"""

FRAGMENT_SHADER = """
#version 120
uniform sampler2D u_base;
uniform sampler2D u_nrm;
uniform sampler2D u_orm;
uniform sampler2D u_envtex;
uniform int u_mode;          // 0 shaded, 1 flat colour (picking, the selection's wireframe)
uniform vec4 u_flat;
uniform int u_pbr;           // the material uses its PBR maps
uniform vec4 u_params;       // roughness, metal, normal, occlusion scales
uniform vec2 u_pbr2;         // highlight (game setting x material), PBR strength
uniform int u_live;          // Live reflections (game setting): the PBR reflection reads the probe
uniform int u_env_mode;      // environment-mapped stage: 0 none, 1 the painted map, 2 live
uniform vec4 u_env;          // its weight (rgb); w 1: it mixes, 0: it adds
uniform vec2 u_env_look;     // live reflection strength, blur 0..1
uniform float u_alpha_ref;   // alpha test threshold, or -1
uniform vec3 u_key_dir;      // towards the key light, view space
uniform vec3 u_key_col;
uniform vec3 u_amb;
uniform mat3 u_to_world;     // view space to world (Z up)
uniform vec4 u_tint;         // the selection's tint, amount in a
varying vec3 v_pos;
varying vec3 v_nrm;
varying vec2 v_uv;

const float PI = 3.14159265;

float band(float x, float half_width, float soft) {
    return 1.0 - smoothstep(half_width - soft, half_width + soft, x);
}

// The studio the reflections show (world space, Z up): sky, horizon, floor, a sun and two panels.
vec3 studio(vec3 d, float blur) {
    d = normalize(d);
    float soft = mix(0.03, 0.9, blur);
    vec3 zenith = vec3(0.30, 0.40, 0.56);
    vec3 horizon = vec3(0.80, 0.81, 0.82);
    vec3 ground = vec3(0.19, 0.17, 0.15);
    vec3 sky = mix(horizon, zenith, sqrt(clamp(d.z, 0.0, 1.0)));
    vec3 c = mix(ground, sky, smoothstep(-soft, soft, d.z));
    vec3 sun = normalize(vec3(0.45, -0.35, 0.82));
    float sb = sqrt(blur);
    c += vec3(1.0, 0.94, 0.82) * pow(max(dot(d, sun), 0.0), mix(900.0, 4.0, sb)) * mix(4.0, 0.45, sb);
    float az = atan(d.y, d.x);
    float panels = band(abs(az - 2.2), 0.28, 0.02 + soft * 0.4) + band(abs(az + 1.9), 0.2, 0.02 + soft * 0.4);
    panels *= band(abs(d.z - 0.2), 0.35, 0.02 + soft * 0.4);
    return c + vec3(0.9) * panels * mix(1.0, 0.3, blur);
}

void main() {
    vec3 dp1 = dFdx(v_pos);
    vec3 dp2 = dFdy(v_pos);
    vec2 duv1 = dFdx(v_uv);
    vec2 duv2 = dFdy(v_uv);
    vec4 tex = texture2D(u_base, v_uv);
    if (u_alpha_ref >= 0.0 && tex.a < u_alpha_ref) {
        discard;
    }
    if (u_mode == 1) {
        gl_FragColor = u_flat;
        return;
    }
    vec3 ng = normalize(gl_FrontFacing ? v_nrm : -v_nrm);
    vec3 v = dot(v_pos, v_pos) > 1e-12 ? normalize(-v_pos) : vec3(0.0, 0.0, 1.0);
    vec3 base = max(tex.rgb, vec3(0.0));
    vec3 l = normalize(u_key_dir);
    // The stage's lit colour L: GX's clamped ambient + diffuse.
    vec3 light_game = clamp(u_amb + max(dot(ng, l), 0.0) * u_key_col, 0.0, 1.0);
    vec3 lit = light_game * base;
    if (u_pbr != 0) {
        vec4 orm = texture2D(u_orm, v_uv);
        vec4 nmap = texture2D(u_nrm, v_uv);
        // GL's window y is up, so dFdy is already Aurora's -dpdy.
        vec3 dp2perp = cross(dp2, ng);
        vec3 dp1perp = cross(ng, dp1);
        vec3 t = dp2perp * duv1.x + dp1perp * duv2.x;
        vec3 b = dp2perp * duv1.y + dp1perp * duv2.y;
        float tlen = max(dot(t, t), dot(b, b));
        vec2 ts0 = (nmap.rg * 1.9921875 - 1.0) * u_params.z;
        vec2 ts = ts0 * min(1.0, 0.999 * inversesqrt(max(dot(ts0, ts0), 1e-8)));
        vec3 n = ng;
        if (tlen > 1e-24) {
            float s = inversesqrt(tlen);
            n = normalize(t * (s * ts.x) - b * (s * ts.y) + ng * sqrt(max(0.0, 1.0 - dot(ts, ts))));
        }
        float ao = mix(1.0, clamp(orm.r, 0.0, 1.0), clamp(u_params.w, 0.0, 1.0));
        float rough = clamp(orm.g * u_params.x, 0.0, 1.0);
        float metal = clamp(orm.b * u_params.y, 0.0, 1.0);
        float a = max(rough, 0.02) * max(rough, 0.02);
        float a2 = a * a;
        float k = (rough + 1.0) * (rough + 1.0) * 0.125;
        float nv = clamp(dot(n, v), 1e-4, 1.0);
        vec3 f0 = mix(vec3(0.04), pow(base, vec3(2.2)), metal);
        // L with the mapped normal (Aurora's pbrs_light).
        vec3 light = clamp(u_amb + max(dot(n, l), 0.0) * u_key_col, 0.0, 1.0);
        float nl = max(dot(n, l), 0.0);
        vec3 h = normalize(l + v);
        float nh = max(dot(n, h), 0.0);
        float vh = max(dot(v, h), 0.0);
        float dd = nh * nh * (a2 - 1.0) + 1.0;
        float d = a2 / max(PI * dd * dd, 1e-6 * a2);
        float vis = 0.25 / (max(nl * (1.0 - k) + k, 1e-4) * max(nv * (1.0 - k) + k, 1e-4));
        vec3 f = f0 + (1.0 - f0) * pow(1.0 - vh, 5.0);
        vec3 e = min(max(u_key_col, vec3(0.0)), vec3(1.0));
        vec3 spec = (PI * d * vis * nl) * f * pow(e, vec3(2.2));
        spec *= min(vec3(1.0), 1.0 / max(e, vec3(1e-4))) * u_pbr2.x;
        vec4 r = vec4(-1.0, -0.0275, -0.572, 0.022) * rough + vec4(1.0, 0.0425, 1.04, -0.04);
        float a004 = min(r.x * r.x, exp2(-9.28 * nv)) * r.x + r.y;
        vec2 ab = vec2(-1.04, 1.04) * a004 + r.zw;
        vec3 envl = pow(light, vec3(2.2));
        if (u_live != 0) {
            vec3 rv = reflect(normalize(v_pos), n);
            vec3 pc = studio(u_to_world * rv, rough);
            float occ = mix(0.2, 1.0, clamp(max(light.r, max(light.g, light.b)) * 2.0, 0.0, 1.0));
            envl = pow(max(pc, vec3(0.0)), vec3(2.2)) * occ;
        }
        vec3 env = envl * (f0 * ab.x + ab.y) * ao;
        vec3 diff = base * (1.0 - metal) * light * ao;
        vec3 pbr = pow(pow(diff, vec3(2.2)) + spec + max(env, vec3(0.0)), vec3(1.0 / 2.2));
        lit = mix(light_game * base, pbr, clamp(u_pbr2.y, 0.0, 1.0));
    }
    if (u_env_mode != 0) {
        vec3 ec;
        if (u_env_mode == 2) {
            vec3 rv = reflect(normalize(v_pos), ng);
            ec = studio(u_to_world * rv, u_env_look.y) * u_env_look.x;
        } else {
            ec = texture2D(u_envtex, vec2(0.5 + 0.5 * ng.x, 0.5 - 0.5 * ng.y)).rgb;
        }
        lit = u_env.w > 0.5 ? mix(lit, ec, u_env.rgb) : lit + ec * u_env.rgb;
    }
    vec3 col = mix(clamp(lit, 0.0, 1.0), u_tint.rgb, u_tint.a);
    gl_FragColor = vec4(col, tex.a);
}
"""

UNIFORMS = ("u_mv", "u_proj", "u_nmat", "u_base", "u_nrm", "u_orm", "u_envtex", "u_mode", "u_flat", "u_pbr",
            "u_params", "u_pbr2", "u_live", "u_env_mode", "u_env", "u_env_look", "u_alpha_ref", "u_key_dir",
            "u_key_col", "u_amb", "u_to_world", "u_tint")
SELECT_TINT = (1.0, 0.55, 0.1)
FLASH_SECONDS = 0.8  # a picked material flashes, then shows as it is
AMBIENT = (0.36, 0.36, 0.38)
KEY_COLOR = (0.82, 0.80, 0.76)
BLEND = {0: "ZERO", 1: "ONE", 2: "SRC_COLOR", 3: "ONE_MINUS_SRC_COLOR", 4: "SRC_ALPHA", 5: "ONE_MINUS_SRC_ALPHA",
         6: "DST_ALPHA", 7: "ONE_MINUS_DST_ALPHA"}


# ------------------------------------------------------------------------------------- the maths
def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _norm(a):
    length = math.sqrt(_dot(a, a)) or 1.0
    return [a[0] / length, a[1] / length, a[2] / length]


def look_at(eye, target, up):
    """The view matrix (rows) looking from `eye` at `target`."""
    f = _norm(_sub(target, eye))
    s = _cross(f, up)
    if _dot(s, s) < 1e-12:
        s = _cross(f, [0.0, 1.0, 0.0])
    s = _norm(s)
    u = _cross(s, f)
    return [[s[0], s[1], s[2], -_dot(s, eye)], [u[0], u[1], u[2], -_dot(u, eye)],
            [-f[0], -f[1], -f[2], _dot(f, eye)], [0.0, 0.0, 0.0, 1.0]]


def perspective(fovy, aspect, near, far):
    f = 1.0 / math.tan(fovy / 2.0)
    return [[f / aspect, 0.0, 0.0, 0.0], [0.0, f, 0.0, 0.0],
            [0.0, 0.0, (far + near) / (near - far), 2.0 * far * near / (near - far)], [0.0, 0.0, -1.0, 0.0]]


def _flat(rows):
    return [v for row in rows for v in row]


class Camera:
    """Turns around `target` at `distance`, Z up (the game's world)."""

    def __init__(self):
        self.target = [0.0, 0.0, 0.0]
        self.distance = 10.0
        self.yaw = math.radians(-60.0)
        self.pitch = math.radians(25.0)
        self.radius = 5.0  # the scene's, for the clip planes and the step sizes

    def eye(self):
        cp = math.cos(self.pitch)
        return [self.target[0] + self.distance * cp * math.cos(self.yaw),
                self.target[1] + self.distance * cp * math.sin(self.yaw),
                self.target[2] + self.distance * math.sin(self.pitch)]

    def view(self):
        return look_at(self.eye(), self.target, [0.0, 0.0, 1.0])

    def clip(self):
        near = max(1e-3, min(self.distance * 0.02, self.radius * 0.005))
        far = self.distance + self.radius * 3.0 + 10.0
        return near, max(far, near * 10.0)

    def frame(self, lo, hi):
        self.target = [(lo[k] + hi[k]) / 2.0 for k in range(3)]
        r = max(0.5 * math.sqrt(sum((hi[k] - lo[k]) ** 2 for k in range(3))), 1e-3)
        # The box's sphere holds the model with room to spare: closer than that sphere's fit.
        self.distance = r / math.sin(FOV / 2.0) * 0.8
        return r

    def axes(self):
        """The view's right, up and forward in the world."""
        v = self.view()
        return v[0][:3], v[1][:3], [-v[2][0], -v[2][1], -v[2][2]]


# ------------------------------------------------------------------------------------ the view
class ModelView(OpenGLFrame):
    """The view. `fields_for(material)` gives a material's materials.json entry, `game` the game's
    settings (pbr_maps, live_reflections, pbr_highlight), `on_pick(index)` hears picks."""

    def __init__(self, master, fields_for, on_pick, game, **kw):
        OpenGLFrame.__init__(self, master, **kw)
        self.fields_for = fields_for
        self.on_pick = on_pick
        self.game = game
        self.scene = None
        self.selected = None
        self.only_selected = False
        self.compare = False  # draw without the edits
        self.wire = False
        self._flash_until = 0.0
        self._tint = 0.0
        self.camera = Camera()
        self.light = [math.radians(-35.0), math.radians(40.0)]  # yaw, pitch from the view
        self.problem = None
        self._ready = False
        self._gpu = None  # {"batches": {index: (buffers, count)}, "textures": {tid: id}, "maps": ...}
        self._pending = None
        self._drag = None
        self._program = None
        self._loc = {}
        self._white = self._flat_normal = None
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonPress-2>", self._press)
        self.bind("<ButtonPress-3>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<B2-Motion>", self._motion)
        self.bind("<B3-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._double)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self.zoom(1))
        self.bind("<Button-5>", lambda e: self.zoom(-1))
        self.bind("<Key>", self._key)
        self.configure(takefocus=1)

    # ---------------------------------------------------------------------------------- GL setup
    def _fail(self, what, error):
        self.problem = "The 3D preview couldn't start OpenGL 2.1 (%s: %s). The editor works without it." % (
            what, error)
        if hasattr(self, "on_problem"):
            self.on_problem(self.problem)

    def tkMap(self, evt):
        if self.problem:
            return
        try:
            OpenGLFrame.tkMap(self, evt)
        except Exception as e:  # noqa: BLE001 - no context: no preview
            self._fail("context", e)

    def tkResize(self, evt):
        if self.problem or not self.context_created:
            self.width, self.height = evt.width, evt.height
            return
        OpenGLFrame.tkResize(self, evt)

    def tkExpose(self, evt):
        if self.problem or not self.context_created:
            return
        OpenGLFrame.tkExpose(self, evt)

    def initgl(self):
        """pyopengltk calls this when the frame shows and again on every resize."""
        GL.glViewport(0, 0, max(self.winfo_width(), 1), max(self.winfo_height(), 1))
        if self._ready or self.problem:
            return
        try:
            self._program = self._link(VERTEX_SHADER, FRAGMENT_SHADER)
            for name in UNIFORMS:
                self._loc[name] = GL.glGetUniformLocation(self._program, name)
            self._white = self._texture(1, 1, b"\xff\xff\xff\xff")
            self._flat_normal = self._texture(1, 1, b"\x80\x80\xff\xff")
            self._ready = True
            version = GL.glGetString(GL.GL_VERSION)
            self.gl_version = version.decode("ascii", "replace") if isinstance(version, bytes) else str(version)
        except Exception as e:  # noqa: BLE001 - shown under the view
            self._fail("shaders", e)
        if self.scene is not None and self._gpu is None and self._ready:
            self._upload()

    @staticmethod
    def _compile(kind, source):
        shader = GL.glCreateShader(kind)
        GL.glShaderSource(shader, source)
        GL.glCompileShader(shader)
        if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
            log = GL.glGetShaderInfoLog(shader)
            raise RuntimeError("shader: %s" % (log.decode("ascii", "replace") if isinstance(log, bytes) else log))
        return shader

    def _link(self, vs, fs):
        program = GL.glCreateProgram()
        GL.glAttachShader(program, self._compile(GL.GL_VERTEX_SHADER, vs))
        GL.glAttachShader(program, self._compile(GL.GL_FRAGMENT_SHADER, fs))
        for i, name in enumerate(("a_pos", "a_nrm", "a_uv")):
            GL.glBindAttribLocation(program, i, name)
        GL.glLinkProgram(program)
        if not GL.glGetProgramiv(program, GL.GL_LINK_STATUS):
            log = GL.glGetProgramInfoLog(program)
            raise RuntimeError("link: %s" % (log.decode("ascii", "replace") if isinstance(log, bytes) else log))
        return program

    @staticmethod
    def _texture(width, height, rgba, repeat=True):
        tid = int(GL.glGenTextures(1))
        GL.glBindTexture(GL.GL_TEXTURE_2D, tid)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        wrap = GL.GL_REPEAT if repeat else GL.GL_CLAMP_TO_EDGE
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, wrap)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, wrap)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        mipmaps = width > 1 or height > 1
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                           GL.GL_LINEAR_MIPMAP_LINEAR if mipmaps else GL.GL_LINEAR)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, width, height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, rgba)
        if mipmaps:
            try:
                GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            except Exception:  # noqa: BLE001 - before GL 3.0: no mipmaps
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        return tid

    @staticmethod
    def _buffer(data):
        bid = int(GL.glGenBuffers(1))
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, bid)
        raw = data.tobytes()
        GL.glBufferData(GL.GL_ARRAY_BUFFER, len(raw), raw, GL.GL_STATIC_DRAW)
        return bid

    def _release_gpu(self):
        if not self._gpu:
            self._gpu = None
            return
        self.tkMakeCurrent()
        for buffers, _count in self._gpu["batches"].values():
            GL.glDeleteBuffers(len(buffers), buffers)
        textures = list(self._gpu["textures"].values())
        for n, o in self._gpu["maps"].values():
            textures += [n, o]
        if textures:
            GL.glDeleteTextures(textures)
        self._gpu = None

    def _upload(self):
        self.tkMakeCurrent()
        scene = self.scene
        gpu = {"batches": {}, "textures": {}, "maps": {}}
        for index, batch in scene.batches.items():
            buffers = [self._buffer(batch.positions), self._buffer(batch.normals), self._buffer(batch.uvs)]
            gpu["batches"][index] = (buffers, batch.count)
        for tid, pic in scene.pictures.items():
            gpu["textures"][tid] = self._texture(pic.width, pic.height, pic.rgba)
        for tid, (n, o) in scene.maps.items():
            gpu["maps"][tid] = (self._texture(n.width, n.height, n.rgba), self._texture(o.width, o.height, o.rgba))
        self._gpu = gpu

    # ------------------------------------------------------------------------------- the scene
    def set_scene(self, scene, frame=True):
        if self._ready:
            self._release_gpu()
        self.scene = scene
        self._gpu = None
        if scene is not None and frame:
            self.frame_all()
        if self._ready and scene is not None:
            self._upload()
        self.request()

    def select(self, index):
        if index != self.selected and index is not None:
            self._flash_until = time.monotonic() + FLASH_SECONDS
        self.selected = index
        self.request()

    def _flash(self):
        """How much the selection's tint shows now (it fades), asking for the next frame while it does."""
        left = self._flash_until - time.monotonic()
        if left <= 0.0:
            return 0.0
        self.request()
        return 0.45 * left / FLASH_SECONDS

    def frame_all(self):
        if self.scene is None:
            return
        box = preview.geometry.bounds(self.scene.batches)
        if box:
            self.camera.radius = self.camera.frame(*box)
        self.request()

    def frame_selected(self):
        if self.scene is None or self.selected not in self.scene.batches:
            return self.frame_all()
        box = preview.geometry.bounds(self.scene.batches, only={self.selected})
        if box:
            radius = self.camera.radius
            self.camera.frame(*box)
            self.camera.radius = max(radius, 1e-3)
        self.request()

    # -------------------------------------------------------------------------------- drawing
    def request(self):
        """Redraw soon (several requests make one redraw)."""
        if self._pending is None:
            try:
                self._pending = self.after(15, self._paint)
            except tk.TclError:
                pass

    def _paint(self):
        self._pending = None
        if self.problem or not self.context_created or not self.winfo_ismapped():
            return
        try:
            self._display()
        except Exception as e:  # noqa: BLE001 - a GL error mustn't take the editor down
            self.problem = "The 3D preview stopped: %s" % e
            if hasattr(self, "on_problem"):
                self.on_problem(self.problem)

    def _matrices(self):
        w, h = max(self.winfo_width(), 1), max(self.winfo_height(), 1)
        near, far = self.camera.clip()
        view = self.camera.view()
        proj = perspective(FOV, w / float(h), near, far)
        rot = [row[:3] for row in view[:3]]
        to_world = [[rot[0][0], rot[1][0], rot[2][0]], [rot[0][1], rot[1][1], rot[2][1]],
                    [rot[0][2], rot[1][2], rot[2][2]]]
        return w, h, view, proj, rot, to_world

    def _key_dir(self):
        yaw, pitch = self.light
        return [math.cos(pitch) * math.sin(yaw), math.sin(pitch), math.cos(pitch) * math.cos(yaw)]

    def redraw(self):
        if not self._ready:
            return
        w, h, view, proj, rot, to_world = self._matrices()
        GL.glViewport(0, 0, w, h)
        GL.glClearColor(0.15, 0.16, 0.18, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if self.scene is None or self._gpu is None:
            return
        self._draw(view, proj, rot, to_world, pick=False)

    def _order(self, view):
        """(opaque, blended far to near) material indices to draw."""
        scene = self.scene
        keys = [k for k in self._gpu["batches"] if not self.only_selected or k == self.selected]
        opaque, blended = [], []
        for k in keys:
            look = scene.looks.get(k)
            (blended if look is not None and look.blended else opaque).append(k)
        if blended:
            def depth(k):
                b = scene.batches[k]
                p = b.positions
                n = len(p) // 3
                step = max(1, n // 64)
                pts = [(p[3 * i], p[3 * i + 1], p[3 * i + 2]) for i in range(0, n, step)]
                c = [sum(q[j] for q in pts) / len(pts) for j in range(3)]
                return view[2][0] * c[0] + view[2][1] * c[1] + view[2][2] * c[2] + view[2][3]
            blended.sort(key=depth)
        return opaque, blended

    def _draw(self, view, proj, rot, to_world, pick):
        loc = self._loc
        GL.glUseProgram(self._program)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDisable(GL.GL_DITHER)
        GL.glUniformMatrix4fv(loc["u_mv"], 1, GL.GL_TRUE, _flat(view))
        GL.glUniformMatrix4fv(loc["u_proj"], 1, GL.GL_TRUE, _flat(proj))
        GL.glUniformMatrix3fv(loc["u_nmat"], 1, GL.GL_TRUE, _flat(rot))
        GL.glUniformMatrix3fv(loc["u_to_world"], 1, GL.GL_TRUE, _flat(to_world))
        for i, name in enumerate(("u_base", "u_nrm", "u_orm", "u_envtex")):
            GL.glUniform1i(loc[name], i)
        GL.glUniform3f(loc["u_key_dir"], *self._key_dir())
        GL.glUniform3f(loc["u_key_col"], *KEY_COLOR)
        GL.glUniform3f(loc["u_amb"], *AMBIENT)
        GL.glUniform1i(loc["u_mode"], 1 if pick else 0)
        for i in range(3):
            GL.glEnableVertexAttribArray(i)
        opaque, blended = self._order(view)
        self._tint = 0.0 if pick or self.only_selected else self._flash()
        GL.glDisable(GL.GL_BLEND)
        GL.glDepthMask(GL.GL_TRUE)
        for k in opaque:
            self._draw_batch(k, pick)
        if blended:
            if not pick:
                GL.glEnable(GL.GL_BLEND)
                GL.glDepthMask(GL.GL_FALSE)
            for k in blended:
                look = self.scene.looks.get(k)
                if not pick:
                    GL.glBlendFunc(getattr(GL, "GL_" + BLEND.get(look.blend_src, "ONE")),
                                   getattr(GL, "GL_" + BLEND.get(look.blend_dst, "ZERO")))
                self._draw_batch(k, pick)
            GL.glDisable(GL.GL_BLEND)
            GL.glDepthMask(GL.GL_TRUE)
        if not pick and self.wire and self.selected in self._gpu["batches"]:
            GL.glUniform1i(loc["u_mode"], 1)
            GL.glUniform4f(loc["u_flat"], SELECT_TINT[0], SELECT_TINT[1], SELECT_TINT[2], 1.0)
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
            GL.glEnable(GL.GL_POLYGON_OFFSET_LINE)
            GL.glPolygonOffset(-1.0, -1.0)
            self._draw_batch(self.selected, True, flat_color=False)
            GL.glDisable(GL.GL_POLYGON_OFFSET_LINE)
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
        for i in range(3):
            GL.glDisableVertexAttribArray(i)
        GL.glUseProgram(0)

    def _use_texture(self, unit, tid):
        GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tid)

    def _draw_batch(self, k, pick, flat_color=True):
        loc = self._loc
        scene = self.scene
        buffers, count = self._gpu["batches"][k]
        look = scene.looks.get(k)
        base_tid = scene.base_texture(k)
        self._use_texture(0, self._gpu["textures"].get(base_tid, self._white))
        GL.glUniform1f(loc["u_alpha_ref"], 64.0 / 255.0 if look is not None and look.alpha_test else -1.0)
        if pick:
            if flat_color:
                v = k + 1
                GL.glUniform4f(loc["u_flat"], (v & 0xFF) / 255.0, ((v >> 8) & 0xFF) / 255.0,
                               ((v >> 16) & 0xFF) / 255.0, 1.0)
        else:
            m = scene.materials[k] if k < len(scene.materials) else None
            fields = {} if self.compare or m is None else (self.fields_for(m) or {})
            maps = self._gpu["maps"].get(base_tid)
            p = preview.params(fields, look, maps is not None, self.game)
            GL.glUniform1i(loc["u_pbr"], 1 if p["pbr"] else 0)
            if p["pbr"]:
                self._use_texture(1, maps[0])
                self._use_texture(2, maps[1])
            else:
                self._use_texture(1, self._flat_normal)
                self._use_texture(2, self._white)
            GL.glUniform4f(loc["u_params"], p["roughness"], p["metal"], p["normal"], p["occlusion"])
            GL.glUniform2f(loc["u_pbr2"], p["highlight"], p["strength"])
            GL.glUniform1i(loc["u_live"], 1 if p["live_probe"] else 0)
            env_mode = {"none": 0, "painted": 1, "live": 2}[p["env"]]
            env_tid = scene.env_texture(k)
            if env_mode == 1 and env_tid not in self._gpu["textures"]:
                env_mode = 0
            GL.glUniform1i(loc["u_env_mode"], env_mode)
            self._use_texture(3, self._gpu["textures"].get(env_tid, self._white))
            weight = look.env_weight if look is not None and look.env_weight else (0.5, 0.5, 0.5)
            GL.glUniform4f(loc["u_env"], weight[0], weight[1], weight[2], 1.0 if look is not None and look.env_mix else 0.0)
            GL.glUniform2f(loc["u_env_look"], p["env_strength"], p["env_blur"])
            amount = self._tint if k == self.selected else 0.0
            GL.glUniform4f(loc["u_tint"], SELECT_TINT[0], SELECT_TINT[1], SELECT_TINT[2], amount)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        for i, (bid, size) in enumerate(zip(buffers, (3, 3, 2))):
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, bid)
            GL.glVertexAttribPointer(i, size, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, count)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

    # --------------------------------------------------------------------------- reading back
    @staticmethod
    def _pixels(x, y, fmt, kind):
        data = GL.glReadPixels(x, y, 1, 1, fmt, kind)
        if hasattr(data, "tobytes"):
            data = data.tobytes()
        return bytes(data)

    def pick(self, x, y):
        """The material index drawn at window pixel (x, y), or None."""
        if not self._ready or self.scene is None or self._gpu is None:
            return None
        self.tkMakeCurrent()
        w, h, view, proj, rot, to_world = self._matrices()
        GL.glViewport(0, 0, w, h)
        GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        self._draw(view, proj, rot, to_world, pick=True)
        GL.glFinish()
        px = self._pixels(x, h - 1 - y, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        self.request()  # the back buffer now holds the ids: draw the view again
        v = px[0] | px[1] << 8 | px[2] << 16
        return v - 1 if v else None

    def point_at(self, x, y):
        """The world point drawn at window pixel (x, y), or None for the background."""
        if not self._ready or self.scene is None or self._gpu is None:
            return None
        self.tkMakeCurrent()
        w, h, view, proj, rot, to_world = self._matrices()
        GL.glViewport(0, 0, w, h)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        self._draw(view, proj, rot, to_world, pick=True)
        GL.glFinish()
        (depth,) = struct.unpack("=f", self._pixels(x, h - 1 - y, GL.GL_DEPTH_COMPONENT, GL.GL_FLOAT)[:4])
        self.request()
        if depth >= 1.0:
            return None
        near, far = self.camera.clip()
        z_ndc = depth * 2.0 - 1.0
        z_eye = 2.0 * far * near / ((far - near) * z_ndc - (far + near))  # negative: in front
        tan = math.tan(FOV / 2.0)
        x_ndc = (x + 0.5) / w * 2.0 - 1.0
        y_ndc = 1.0 - (y + 0.5) / h * 2.0
        ex, ey = -z_eye * x_ndc * tan * (w / float(h)), -z_eye * y_ndc * tan
        right, up, forward = self.camera.axes()
        eye = self.camera.eye()
        return [eye[k] + right[k] * ex + up[k] * ey + forward[k] * -z_eye for k in range(3)]

    # ---------------------------------------------------------------------------------- input
    def _press(self, event):
        self.focus_set()
        self._drag = (event.num, event.x, event.y, event.x, event.y, bool(event.state & 0x1))

    def _motion(self, event):
        if self._drag is None:
            return
        num, x0, y0, lx, ly, shift = self._drag
        dx, dy = event.x - lx, event.y - ly
        self._drag = (num, x0, y0, event.x, event.y, shift)
        if num == 1 and shift:
            self.light[0] += dx * 0.01
            self.light[1] = max(-1.5, min(1.5, self.light[1] - dy * 0.01))
        elif num == 1:
            self.camera.yaw -= dx * 0.01
            self.camera.pitch = max(-1.55, min(1.55, self.camera.pitch + dy * 0.01))
        else:
            h = max(self.winfo_height(), 1)
            scale = 2.0 * self.camera.distance * math.tan(FOV / 2.0) / h
            right, up, _f = self.camera.axes()
            for k in range(3):
                self.camera.target[k] -= (right[k] * dx - up[k] * dy) * scale
        self.request()

    def _release(self, event):
        drag, self._drag = self._drag, None
        if drag is None or abs(event.x - drag[1]) + abs(event.y - drag[2]) > 3 or drag[5]:
            return
        index = self.pick(event.x, event.y)
        if index is not None:
            self.on_pick(index)

    def _double(self, event):
        point = self.point_at(event.x, event.y)
        if point is not None:
            eye = self.camera.eye()
            self.camera.target = point
            d = _sub(eye, point)
            self.camera.distance = max(math.sqrt(_dot(d, d)), 1e-3)
            self.camera.pitch = math.asin(max(-1.0, min(1.0, d[2] / self.camera.distance)))
            self.camera.yaw = math.atan2(d[1], d[0])
            self.request()

    def zoom(self, steps):
        self.camera.distance = max(1e-3, self.camera.distance * (0.85 ** steps))
        self.request()

    def _wheel(self, event):
        self.zoom(event.delta / 120.0 if abs(event.delta) >= 120 else (1 if event.delta > 0 else -1))

    def _key(self, event):
        key = event.keysym.lower()
        if key == "f":
            self.frame_selected()
            return
        if key == "home":
            self.frame_all()
            return
        moves = {"w": (0, 1), "s": (0, -1), "a": (1, -1), "d": (1, 1), "q": (2, -1), "e": (2, 1)}
        if key not in moves:
            return
        axis, sign = moves[key]
        right, _up, forward = self.camera.axes()
        direction = [forward, right, [0.0, 0.0, 1.0]][axis]
        step = max(self.camera.distance * 0.08, self.camera.radius * 0.005) * (4.0 if event.state & 0x1 else 1.0)
        for k in range(3):
            self.camera.target[k] += direction[k] * step * sign
        self.request()


def available():
    """None when the preview can run, else why not."""
    return PROBLEM
