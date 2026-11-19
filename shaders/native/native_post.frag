#version 450
// NATIVE RENDERER, the finishing pass over the whole picture (before the HUD):
// SKY: drifting clouds, stars and a glowing moon where nothing was drawn in front of the game's sky (depth 0),
// light shafts streaming from the moon past whatever stands in front of it, lightning flashes, then
// COLOUR GRADING: a night-time contrast curve and colour balance, and a soft vignette.
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 oColor;
layout(set = 0, binding = 0) uniform sampler2D uColor;
layout(set = 0, binding = 1) uniform sampler2D uDepth;
// a: screen x scale, screen y scale, half width, half height
// b: camera position (world), time in seconds
// r0, r1, r2: the camera's right, up and forward directions (xyz); their w: the moon's direction (world)
// m: the moon on screen (uv), see below, lightning (0..1)
// g: grading strength, vignette strength, clouds, stars
// (m.z: (1 + light shaft strength), negative when the moon is behind the camera)
// s: x reflection strength (0: off), y depth of field strength (0: off), z 1 if the game draws its own moon,
//    w heat haze strength (0: off)
layout(push_constant) uniform PC { vec4 a; vec4 b; vec4 r0; vec4 r1; vec4 r2; vec4 m; vec4 g; vec4 s; } pc;

vec3 viewPos(vec2 uv, float d)
{
    float w = 1.0 / d;
    vec2 ndc = uv * 2.0 - 1.0;
    return vec3(ndc.x * pc.a.z * w / pc.a.x, ndc.y * pc.a.w * w / pc.a.y, w);
}

vec2 project(vec3 p)
{
    vec2 ndc = vec2(p.x * pc.a.x / (p.z * pc.a.z), p.y * pc.a.y / (p.z * pc.a.w));
    return ndc * 0.5 + 0.5;
}

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

float fbm(vec2 p)
{
    float s = 0.0, a = 0.5;
    for (int i = 0; i < 4; ++i)
    {
        s += a * noise2(p);
        p = p * 2.03 + vec2(17.1, 9.7);
        a *= 0.5;
    }
    return s;
}

float lum(vec3 c)
{
    return dot(c, vec3(0.299, 0.587, 0.114));
}

void main()
{
    vec4 src = texture(uColor, vUV);
    if (pc.s.w > 0.0)
    {
        // HEAT HAZE: the air over lava and fire shimmers. Where the picture just below (or here) is glowing
        // red-orange, the picture is bent by a slowly rising ripple.
        float heat = 0.0;
        for (int k = 0; k < 4; ++k)
        {
            vec2 q = vUV + vec2(0.0, 0.012 + 0.03 * float(k));
            vec3 c = texture(uColor, q).rgb;
            float hot = smoothstep(0.3, 0.55, c.r) * smoothstep(1.6, 2.6, c.r / max(c.g, 0.02)) * step(c.b * 2.5, c.r);
            heat = max(heat, hot * (1.0 - 0.18 * float(k)));
        }
        if (heat > 0.0)
        {
            float t = pc.b.w;
            vec2 w = vec2(noise2(vUV * vec2(60.0, 30.0) + vec2(0.0, t * 2.2)) - 0.5,
                          noise2(vUV * vec2(55.0, 28.0) + vec2(7.3, t * 2.6)) - 0.5);
            vec2 uv2 = vUV + w * 0.006 * heat * pc.s.w;
            src = vec4(texture(uColor, uv2).rgb, src.a);
        }
    }
    vec3 col = src.rgb;
    float d = texture(uDepth, vUV).r;
    // DEPTH OF FIELD: what is well behind Maximo goes softly out of focus. The focus is the distance to what is
    // in the middle of the screen, a little low (Maximo); the blur grows with distance beyond it, gathered from a
    // ring of samples that are themselves far (so nothing near bleeds into the blur).
    if (pc.s.y > 0.0)
    {
        float fsum = 0.0, fn = 0.0;
        for (int k = 0; k < 5; ++k)
        {
            vec2 fo = vec2(0.5, 0.56) + vec2(float(k % 3) - 1.0, float(k / 3) - 0.5) * vec2(0.04, 0.05);
            float fd = texture(uDepth, fo).r;
            if (fd > 1e-6)
            {
                // the farthest of them: grass or a leaf right in front of the camera must not pull the focus in
                fsum = max(fsum, 1.0 / fd);
                fn += 1.0;
            }
        }
        float focus = fn > 0.0 ? max(fsum, 8.0) : 15.0;
        float w = d > 1e-6 ? 1.0 / d : 1e4;
        float coc = smoothstep(focus * 2.0, focus * 6.0, w) * pc.s.y;
        if (coc > 0.01)
        {
            vec2 px = 1.0 / vec2(textureSize(uColor, 0));
            float radius = coc * 7.0;
            vec3 sum = col;
            float wsum = 1.0;
            const vec2 disk[12] = vec2[](vec2(-0.326, -0.406), vec2(-0.840, -0.074), vec2(-0.696, 0.457),
                                         vec2(-0.203, 0.621), vec2(0.962, -0.195), vec2(0.473, -0.480),
                                         vec2(0.519, 0.767), vec2(0.185, -0.893), vec2(0.507, 0.064),
                                         vec2(0.896, 0.412), vec2(-0.322, -0.933), vec2(-0.792, -0.598));
            float rot = hash(gl_FragCoord.xy) * 6.2831853;
            mat2 rm = mat2(cos(rot), sin(rot), -sin(rot), cos(rot));
            for (int k = 0; k < 12; ++k)
            {
                vec2 o = rm * disk[k] * radius * px * 2.0;
                float sd = texture(uDepth, vUV + o).r;
                float sw = sd > 1e-6 ? 1.0 / sd : 1e4;
                float wk = smoothstep(focus * 1.2, focus * 2.0, sw); // only far things
                sum += texture(uColor, vUV + o).rgb * wk;
                wsum += wk;
            }
            col = sum / wsum;
        }
    }
    // REFLECTIONS: on what is reflective (the picture's alpha: water, metal, wet ground), follow the reflected line
    // of sight through the depth buffer until it passes behind something drawn, and take that pixel's colour;
    // where it leaves the picture, the night sky. Stronger at grazing angles, as on real water.
    if (pc.s.x > 0.0 && src.a > 0.15 && d > 1e-6)
    {
        vec3 P = viewPos(vUV, d);
        vec2 tsz = 1.0 / vec2(textureSize(uDepth, 0));
        vec2 tx = vec2(tsz.x, 0.0), ty = vec2(0.0, tsz.y);
        float dl = texture(uDepth, vUV - tx).r, dr = texture(uDepth, vUV + tx).r;
        float du = texture(uDepth, vUV - ty).r, dd = texture(uDepth, vUV + ty).r;
        vec3 Pl = viewPos(vUV - tx, max(dl, 1e-6)), Pr = viewPos(vUV + tx, max(dr, 1e-6));
        vec3 Pu = viewPos(vUV - ty, max(du, 1e-6)), Pd = viewPos(vUV + ty, max(dd, 1e-6));
        vec3 ex = (abs(Pr.z - P.z) < abs(P.z - Pl.z)) ? Pr - P : P - Pl;
        vec3 ey = (abs(Pd.z - P.z) < abs(P.z - Pu.z)) ? Pd - P : P - Pu;
        vec3 N = normalize(cross(ex, ey));
        vec3 V = normalize(P);
        if (dot(N, V) > 0.0)
            N = -N;
        vec3 R = reflect(V, N);
        vec3 hitCol = vec3(0.045, 0.05, 0.07); // (the sky, if nothing is hit)
        float found = 0.0;
        float jit = hash(gl_FragCoord.xy);
        float tPrev = 0.0;
        for (int i = 1; i <= 18; ++i)
        {
            float tr = 0.5 * pow(float(i) + jit, 1.5);
            vec3 S = P + R * tr;
            if (S.z <= 1.0)
                break;
            vec2 uv = project(S);
            if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0)
                break;
            float ds = texture(uDepth, uv).r;
            if (ds > 1e-6)
            {
                float sz = 1.0 / ds;
                float thick = max(0.6, (tr - tPrev) * 1.5);
                if (S.z > sz && S.z - sz < thick)
                {
                    // narrow it down between the last step and this one
                    float lo = tPrev, hi = tr;
                    for (int k = 0; k < 5; ++k)
                    {
                        float mid = 0.5 * (lo + hi);
                        vec3 M = P + R * mid;
                        vec2 muv = project(M);
                        float md2 = texture(uDepth, muv).r;
                        if (md2 > 1e-6 && M.z > 1.0 / md2)
                            hi = mid;
                        else
                            lo = mid;
                    }
                    vec2 huv = project(P + R * hi);
                    vec2 edge = smoothstep(vec2(0.0), vec2(0.08), huv) * smoothstep(vec2(0.0), vec2(0.08), 1.0 - huv);
                    hitCol = texture(uColor, huv).rgb;
                    found = edge.x * edge.y * (1.0 - float(i) / 20.0);
                    break;
                }
            }
            tPrev = tr;
        }
        vec3 sky = vec3(0.045, 0.05, 0.07);
        vec3 reflCol = mix(sky, hitCol, found);
        float fres = 0.08 + 0.92 * pow(1.0 - max(dot(-V, N), 0.0), 4.0);
        float amount = clamp(src.a * mix(0.45, 1.0, fres) * pc.s.x, 0.0, 0.9);
        col = mix(col, reflCol, amount);
    }
    vec2 ndc = vUV * 2.0 - 1.0;
    vec3 dirV = vec3(ndc.x * pc.a.z / pc.a.x, ndc.y * pc.a.w / pc.a.y, 1.0);
    vec3 dir = normalize(pc.r0.xyz * dirV.x + pc.r1.xyz * dirV.y + pc.r2.xyz * dirV.z);
    vec3 moonDir = normalize(vec3(pc.r0.w, pc.r1.w, pc.r2.w));
    float t = pc.b.w;
    float flash = pc.m.w;
    float shafts = abs(pc.m.z) - 1.0;
    bool moonOn = pc.m.z > 0.0;
    float md = max(dot(dir, moonDir), 0.0);
    if (d <= 1e-6)
    {
        // SKY: richer night colour first (a little deeper and bluer)
        float l = lum(col);
        // where the game's own sky is bright (its moon, lit clouds), the added clouds, stars and moon stay off it
        float gameBright = smoothstep(0.12, 0.3, l);
        // (the night tint too: the game's moon keeps its own colour)
        col = mix(col, vec3(l * 0.75, l * 0.85, l * 1.2), 0.35 * pc.g.x * (1.0 - gameBright));
        float up = max(dir.y, 0.0);
        // stars: points on a grid of directions, twinkling, only where the sky is dark
        if (pc.g.w > 0.0 && up > 0.02)
        {
            vec2 sp = dir.xz / (dir.y + 0.35) * 220.0;
            vec2 cell = floor(sp);
            float h = hash(cell);
            if (h > 0.985)
            {
                vec2 c = cell + 0.5 + (vec2(hash(cell + 3.1), hash(cell + 7.7)) - 0.5) * 0.6;
                float r = length(sp - c);
                float tw = 0.6 + 0.4 * sin(t * (2.0 + 3.0 * h) + h * 50.0);
                float star = smoothstep(0.55, 0.0, r) * tw * (h - 0.985) / 0.015;
                col += vec3(0.85, 0.9, 1.0) * star * pc.g.w * smoothstep(0.35, 0.05, l) * smoothstep(0.02, 0.15, up);
            }
        }
        // the moon: a bright disc with faint markings, and a wide soft glow around it
        float disc = smoothstep(0.99955, 0.9998, md);
        float marks = 0.85 + 0.15 * noise2(dir.xz * 900.0);
        vec3 moonCol = vec3(0.95, 0.96, 1.0) * marks;
        // (not when the game draws its own moon: one moon)
        float addMoon = (pc.g.z > 0.0 ? 1.0 : 0.0) * (1.0 - gameBright) * (1.0 - pc.s.z);
        col += (vec3(0.35, 0.4, 0.5) * pow(md, 300.0) * 0.6 + vec3(0.2, 0.24, 0.32) * pow(md, 40.0) * 0.35) * addMoon;
        col = mix(col, moonCol, disc * addMoon);
        // clouds: a drifting layer high above, thick in patches, lit silver at the edges towards the moon and
        // bright inside when the lightning flashes; thinner towards the horizon (where the mist is)
        if (pc.g.z > 0.0 && dir.y > 0.0)
        {
            vec2 cp = dir.xz / (dir.y + 0.12) * 1.6 + vec2(t * 0.012, t * 0.005);
            float n = fbm(cp);
            float cover = smoothstep(0.42, 0.72, n) * pc.g.z;
            float edge = smoothstep(0.72, 0.45, n) * cover;
            vec3 cloud = vec3(0.07, 0.08, 0.10) + vec3(0.25, 0.27, 0.32) * pow(md, 6.0) * edge * 2.0 +
                         vec3(0.6, 0.65, 0.8) * flash * (0.4 + 0.6 * n);
            float a = cover * smoothstep(0.0, 0.25, dir.y) * 0.9 * (1.0 - gameBright);
            col = mix(col, cloud, a);
        }
    }
    // LIGHT SHAFTS: march from this pixel towards the moon on screen, gathering how much open sky there is on
    // the way (anything drawn in front blocks it), so light streams out between trees and gravestones
    if (shafts > 0.0 && moonOn)
    {
        vec2 delta = (pc.m.xy - vUV) / 16.0;
        vec2 uv = vUV;
        float sum = 0.0, wgt = 1.0;
        float jitter = hash(gl_FragCoord.xy + t);
        uv += delta * jitter;
        for (int i = 0; i < 16; ++i)
        {
            uv += delta;
            if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0)
                break;
            float open = texture(uDepth, uv).r <= 1e-6 ? 1.0 : 0.0;
            sum += open * wgt;
            wgt *= 0.9;
        }
        float facing = pow(md, 4.0); // strongest looking towards the moon
        col += vec3(0.42, 0.47, 0.6) * (sum / 16.0) * facing * shafts * 0.6;
    }
    // LIGHTNING: the whole scene lit up for a moment, bluish
    col += (col * 1.6 + vec3(0.05, 0.06, 0.09)) * flash;
    // COLOUR GRADING: a gentle S-curve for contrast, cooler shadows and slightly warmer highlights
    if (pc.g.x > 0.0)
    {
        // (lifted a little first: the night was coming out too dark)
        vec3 c = clamp(col * 1.1 + 0.01, 0.0, 1.0);
        vec3 s = c * c * (3.0 - 2.0 * c);
        vec3 graded = mix(c, s, 0.18);
        float l = lum(graded);
        graded += (vec3(-0.012, 0.0, 0.025) * (1.0 - l) + vec3(0.015, 0.008, -0.01) * l);
        graded = mix(vec3(l), graded, 1.08); // a touch more colour
        col = mix(col, graded, pc.g.x);
    }
    // VIGNETTE: the corners a little darker, drawing the eye to the middle
    vec2 v = vUV - 0.5;
    col *= 1.0 - pc.g.y * smoothstep(0.35, 0.85, length(v * vec2(1.25, 1.0)));
    oColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
