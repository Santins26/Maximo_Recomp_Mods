#version 450
// NATIVE RENDERER, ground fog: a thin mist lying on the ground, thickest at the ground and thinning out a couple of
// units up, drifting slowly. Each pixel's world position comes back from the depth buffer (reversed: 1/w, 0 where
// nothing was drawn); the mist along the line of sight is worked out exactly for a density that falls off
// exponentially with height, then broken up into drifting patches.
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 oColor;
layout(set = 0, binding = 0) uniform sampler2D uDepth;
// a: screen x scale, screen y scale, half width, half height
// b: camera position (world), time in seconds
// r0, r1, r2: the camera's right, up and forward directions in the world (view x, y, z)
// f: ground height, how fast the mist thins with height (units), density at the ground, most it can hide (0..1)
// c: mist colour (rgb 0..1), w: how thick the low drifting wisps are (0: none)
// m: Maximo's feet (x, z), y 1 if known, w: distance haze strength (0: none)
layout(push_constant) uniform PC { vec4 a; vec4 b; vec4 r0; vec4 r1; vec4 r2; vec4 f; vec4 c; vec4 m; } pc;

float hash(vec2 p)
{
    vec3 q = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    q += dot(q, q.yzx + 33.33);
    return fract((q.x + q.y) * q.z);
}

float noise2(vec2 p)
{
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
}

void main()
{
    float d = texture(uDepth, vUV).r;
    vec2 ndc = vUV * 2.0 - 1.0;
    // the view-space direction through this pixel (at w = 1), then the distance along it
    vec3 dirV = vec3(ndc.x * pc.a.z / pc.a.x, ndc.y * pc.a.w / pc.a.y, 1.0);
    // nothing drawn there, looking down (into a pit, off a ledge): no mist (it filled the empty space below the
    // ground with a glowing white column)
    if (d <= 1e-6 && dot(pc.r0.xyz * dirV.x + pc.r1.xyz * dirV.y + pc.r2.xyz * dirV.z, vec3(0.0, 1.0, 0.0)) < 0.05)
    {
        oColor = vec4(pc.c.rgb, 0.0);
        return;
    }
    float w = d > 1e-6 ? 1.0 / d : 160.0; // the sky: as if a long way off
    w = min(w, 160.0);
    vec3 pv = dirV * w;
    vec3 cam = pc.b.xyz;
    vec3 P = cam + pc.r0.xyz * pv.x + pc.r1.xyz * pv.y + pc.r2.xyz * pv.z;
    float L = length(P - cam);
    float H = pc.f.y;
    // (the ground the mist lies on: the surface seen here, kept near Maximo's ground, so a pit or a raised part of
    // the level does not get a sheet of mist cut off at its edge)
    float g = d > 1e-6 ? clamp(P.y, pc.f.x - 2.0, pc.f.x + 1.0) : pc.f.x;
    float yc = cam.y - g, yp = P.y - g;
    // (below the ground, in pits, no thicker than at the ground itself)
    float fc = exp(-max(yc, 0.0) / H), fp = exp(-max(yp, 0.0) / H);
    float dy = yp - yc;
    float column = abs(dy) > 1e-3 ? (fc - fp) * H / dy : fc; // the average density along the line
    // drifting patches, sampled where the line meets the mist (near the far end, low down)
    vec2 at = mix(cam.xz, P.xz, 0.85);
    float t = pc.b.w;
    float n = noise2(at * 0.06 + vec2(t * 0.05, t * 0.02)) * 0.6 + noise2(at * 0.17 - vec2(t * 0.03, t * 0.07)) * 0.4;
    float amount = pc.f.z * column * L * (0.35 + 1.3 * n);
    float a = min(1.0 - exp(-amount), pc.f.w);
    // WISPS: a low layer of mist (the first two units over the ground) in drifting tendrils, gathered along the
    // line of sight where it passes through that layer. Around Maximo's legs it parts and swirls.
    float wisp = 0.0;
    if (pc.c.w > 0.0 && d > 1e-6)
    {
        // (the layer lies on whatever surface is seen here, not on one flat height for the whole picture: that
        // filled the drops below a ledge with a sheet of mist ending in a hard line, and changed with the place)
        float base = min(P.y, pc.f.x + 1.0);
        float top = base + 2.2, bot = base - 0.3;
        vec3 D = P - cam;
        float s0 = 0.0, s1 = 1.0;
        if (abs(D.y) > 1e-4)
        {
            float ta = (top - cam.y) / D.y, tb = (bot - cam.y) / D.y;
            s0 = clamp(min(ta, tb), 0.0, 1.0);
            s1 = clamp(max(ta, tb), 0.0, 1.0);
        }
        else if (cam.y > top || cam.y < bot)
            s1 = s0;
        float seg = (s1 - s0) * L;
        if (seg > 0.01)
        {
            float acc = 0.0;
            for (int k = 0; k < 6; ++k)
            {
                float sk = mix(s0, s1, (float(k) + 0.5) / 6.0);
                vec3 X = cam + D * sk;
                vec2 q = X.xz;
                float clear = 1.0;
                if (pc.m.y > 0.5)
                {
                    vec2 rel = q - pc.m.xy;
                    float r = length(rel);
                    // swirled round him (the closer, the more), and thinned out right around his legs
                    float ang = 1.4 * exp(-r / 2.0) * sin(t * 0.6 + r);
                    float cs = cos(ang), sn = sin(ang);
                    q = pc.m.xy + vec2(cs * rel.x - sn * rel.y, sn * rel.x + cs * rel.y);
                    clear = smoothstep(0.5, 2.0, r);
                }
                float hgt = clamp((X.y - base) / 2.2, 0.0, 1.0);
                float tendril = noise2(q * 0.32 + vec2(t * 0.22, t * 0.09)) * 0.65 +
                                noise2(q * 0.9 - vec2(t * 0.15, -t * 0.27)) * 0.35;
                acc += smoothstep(0.35, 0.72, tendril) * (1.0 - hgt) * (1.0 - hgt) * clear;
            }
            wisp = 1.0 - exp(-acc / 6.0 * min(seg, 6.0) * 0.7 * pc.c.w);
            wisp = min(wisp, 0.32);
        }
    }
    // DISTANCE HAZE: what is far off fades into a cold blue-grey (not the sky: it is far already)
    float haze = 0.0;
    if (pc.m.w > 0.0 && d > 1e-6)
        haze = min((1.0 - exp(-max(L - 30.0, 0.0) * 0.011)) * pc.m.w, 0.38);
    float A = 1.0 - (1.0 - a) * (1.0 - wisp) * (1.0 - haze);
    vec3 hazeCol = pc.c.rgb * vec3(0.92, 1.0, 1.15) + vec3(0.01, 0.015, 0.03);
    vec3 col = A > 1e-4 ? (pc.c.rgb * (a + wisp) + hazeCol * haze) / max(a + wisp + haze, 1e-4) : pc.c.rgb;
    oColor = vec4(col, A);
}
