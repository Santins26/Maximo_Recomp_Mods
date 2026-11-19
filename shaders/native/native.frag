#version 450
// NATIVE RENDERER: the GS texture function (MODULATE), the alpha test, the output alpha scaled for blending, and
// the real-time shadows. Colours are in the GS's 0..255 scale, alphas in its 0..128 scale.
layout(location = 0) in vec2 vUV;
layout(location = 1) in vec4 vCol;
layout(location = 2) in vec3 vWorld;
layout(location = 3) in vec3 vNormal;
layout(location = 4) in vec3 vRaw;
layout(location = 5) in vec3 vMNormal;
// LIGHTING: the game's lights for this draw (LD: model-space light directions, LC: their colours and the ambient,
// as the VU1 code uses them: d = max((n, 1) * LD, 0), colour = d * LC), and the highlight settings.
layout(std140, set = 0, binding = 2) uniform Lights
{
    mat4 LD;
    mat4 LC;
    vec4 flags; // x: per pixel, y: highlight strength, z: sun shading on, w: sun ambient (the far side's brightness)
    vec4 cam;   // camera position (world), w: sun strength
    vec4 sun;   // the direction the sunlight travels (world)
    vec4 point; // x: how many point lights (fire, torches, glows, lava), y: their strength, z: brightness of
                // this draw (lava drawn brighter), w: time in seconds (the flicker)
    vec4 pointPos[16]; // xyz, radius
    vec4 pointCol[16]; // rgb, how fast it flickers
    vec4 extra; // x: surface relief strength (0: none), y: moss strength (0: none), z: cracks and stains on
                // stone (0: none)
    vec4 extra2; // x: grime strength (0: none), y: the ground's height, z: carved depth strength, w: 1 if y is known
    vec4 extra3; // x: metal shine strength, y: window / candle glow strength, z: how reflective (water), w: 1 = write
                 // the reflectivity to the picture's alpha (for the reflection pass)
    vec4 moon;   // xyz: the direction towards the moon
    vec4 weather; // x: how wet everything is (rain), y: puddles, z: how much fire flickers (0: steady),
                  // w: moonlit rim on characters (0: none)
    vec4 look;    // x: what this draw is: 0 anything, 1 a HUD piece (sharper), 2 a HUD piece's soft shadow, 3 the HUD's
                  // soul orb, 4 a gold HUD frame, 5 Maximo, 6 an enemy (its eyes glow), 7 a ghost; y: (enemies) the
                  // height above which the head is; z: (enemies) the eyes' colour (0 red, 1 green, 2 cyan)
} u;
layout(location = 0) out vec4 oColor;
layout(set = 0, binding = 0) uniform sampler2D uTex;
layout(set = 0, binding = 1) uniform sampler2D uShadow;
// a: x textured, y ATST, z AREF, w ATE.  b: x texture alpha scale (255 textures, 128 render targets),
// y receives shadows, z shadow strength, w 1 / shadow map size.  c: the direction the light travels.
// light: world -> shadow map (xy -1..1, z 0..1).
layout(push_constant) uniform PC { vec4 a; vec4 b; vec4 c; mat4 light; } pc;

vec3 hash3(vec2 p)
{
    vec3 q = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    q += dot(q, q.yxz + 33.33);
    return fract((q.xxy + q.yzz) * q.zyx);
}

float noise2(vec2 p)
{
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash3(i).x, b = hash3(i + vec2(1, 0)).x, c = hash3(i + vec2(0, 1)).x, d = hash3(i + vec2(1, 1)).x;
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float lum(vec3 c)
{
    return dot(c, vec3(0.299, 0.587, 0.114));
}

// the distance to the nearest edge between random cells (small on the cracks)
float cellEdge(vec2 p)
{
    vec2 i = floor(p), f = fract(p);
    float d1 = 8.0, d2 = 8.0;
    for (int y = -1; y <= 1; ++y)
        for (int x = -1; x <= 1; ++x)
        {
            vec2 g = vec2(x, y);
            vec2 o = hash3(i + g).xy;
            float d = length(g + o - f);
            if (d < d1)
            {
                d2 = d1;
                d1 = d;
            }
            else if (d < d2)
                d2 = d;
        }
    return d2 - d1;
}

float lit()
{
    vec4 c = pc.light * vec4(vWorld, 1.0);
    vec2 uv = c.xy * 0.5 + 0.5;
    if (any(lessThan(uv, vec2(0.0))) || any(greaterThan(uv, vec2(1.0))) || c.z <= 0.0 || c.z >= 1.0)
        return 1.0;
    float sum = 0.0;
    for (int y = -2; y <= 2; ++y)
        for (int x = -2; x <= 2; ++x)
            sum += (c.z - 0.002 <= texture(uShadow, uv + vec2(x, y) * (1.5 * pc.b.w)).r) ? 1.0 : 0.0;
    // fade out towards the edge of the shadow area
    vec2 e = smoothstep(vec2(0.0), vec2(0.08), uv) * smoothstep(vec2(0.0), vec2(0.08), 1.0 - uv);
    return mix(1.0, sum / 25.0, e.x * e.y);
}

void main()
{
    vec3 vrgb = vCol.rgb;
    if (u.flags.x > 0.5 && length(vMNormal) > 0.01)
    {
        // the same lighting the game does per vertex, per pixel (rows uploaded as stored, so M * v = v * rows)
        vec4 d = u.LD * vec4(normalize(vMNormal), 1.0);
        d = vec4(max(d.xyz, vec3(0.0)), 1.0);
        vrgb = vRaw * max((u.LC * d).rgb, vec3(0.0));
    }
    vec3 rgb;
    float A;
    vec3 texel = vec3(0.0);
    bool hasN = length(vNormal) > 0.5;
    vec3 N = hasN ? normalize(vNormal) : vec3(0.0);
    // (derivatives taken here, outside any branch that can differ between neighbouring pixels)
    vec3 dp1 = dFdx(vWorld), dp2 = dFdy(vWorld);
    vec2 duv1 = dFdx(vUV), duv2 = dFdy(vUV);
    float texLod = max(textureQueryLod(uTex, vUV).y, 0.0);
    if (pc.a.x > 0.5)
    {
        // the surface's own directions from how the texture runs across the screen: gu, gv = how fast u and v
        // change per world unit along the surface
        vec3 gu = vec3(0.0), gv = vec3(0.0);
        bool frame = false;
        if ((u.extra.x > 0.0 || u.extra2.z > 0.0) && hasN)
        {
            vec3 dp2perp = cross(dp2, N), dp1perp = cross(N, dp1);
            float det = dot(dp1, dp2perp);
            if (abs(det) > 1e-14)
            {
                gu = (dp2perp * duv1.x + dp1perp * duv2.x) / det;
                gv = (dp2perp * duv1.y + dp1perp * duv2.y) / det;
                frame = true;
            }
        }
        vec2 uvP = vUV;
        float camDist = length(u.cam.xyz - vWorld);
        if (frame && u.extra2.z > 0.0 && camDist < 20.0)
        {
            // CARVED DEPTH (parallax occlusion): the dark parts of the texture (mortar, cracks) taken as deep, the
            // light parts as the surface; the line of sight is followed into that relief in ten steps, and the
            // texture is read where it first hits, so the stones hide the mortar behind them as you walk past
            vec3 V = normalize(u.cam.xyz - vWorld);
            float vn = max(dot(V, N), 0.3);
            vec3 Vt = V - N * dot(V, N);
            float depth = u.extra2.z * 0.12 * (1.0 - smoothstep(14.0, 20.0, camDist));
            vec3 D = -Vt / vn * depth;
            vec2 duvMax = vec2(dot(gu, D), dot(gv, D));
            float prevLayer = 0.0, prevSurf = 0.0;
            vec2 hit = vUV;
            for (int i = 0; i <= 6; ++i)
            {
                float layer = float(i) / 6.0;
                vec2 uvi = vUV + duvMax * layer;
                float surf = 1.0 - clamp(lum(textureLod(uTex, uvi, texLod).rgb) * 3.0, 0.0, 1.0);
                if (layer >= surf)
                {
                    // between this step and the one before, where the line crossed the relief
                    float a = (prevSurf - prevLayer), b = (layer - surf);
                    float f = i == 0 ? 0.0 : a / max(a + b, 1e-4);
                    hit = vUV + duvMax * mix(prevLayer, layer, f);
                    break;
                }
                prevLayer = layer;
                prevSurf = surf;
                hit = uvi;
            }
            uvP = hit;
        }
        vec4 t = textureGrad(uTex, uvP, duv1, duv2);
        int lookMode = int(u.look.x + 0.5);
        if (lookMode == 1 || lookMode == 3 || lookMode == 4)
        {
            // SHARPER HUD: the small HUD textures blown up smoothly but with crisp edges: between texels a steep,
            // smooth step instead of a long linear ramp (no blur, no blocky pixels)
            vec2 ts = vec2(textureSize(uTex, 0));
            vec2 pp = uvP * ts - 0.5;
            vec2 i0 = floor(pp), f = fract(pp);
            f = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
            t = textureLod(uTex, (i0 + f + 0.5) / ts, 0.0);
        }
        else if (lookMode == 2)
        {
            // the HUD piece's SOFT SHADOW: its shape, blurred (eight reads around), dark
            vec2 ts = 1.0 / vec2(textureSize(uTex, 0));
            float a = 0.0;
            for (int k = 0; k < 8; ++k)
            {
                float an = float(k) * 0.7853982;
                a += textureLod(uTex, uvP + vec2(cos(an), sin(an)) * ts * 1.8, 0.0).a;
            }
            t = vec4(0.0, 0.0, 0.0, a / 8.0);
        }
        texel = t.rgb;
        if (u.extra.x > 0.0 && frame)
        {
            // SURFACE RELIEF: the texture's brightness taken as height (cracks and mortar dark and deep, stones
            // light and raised); its slope across the surface tilts the normal, so the sun and the torches pick out
            // the bumps
            vec2 ts = 1.0 / vec2(textureSize(uTex, 0));
            float lod = texLod;
            vec2 step2 = ts * exp2(lod);
            float h0 = lum(t.rgb);
            float hu = lum(textureLod(uTex, uvP + vec2(step2.x, 0.0), lod).rgb);
            float hv = lum(textureLod(uTex, uvP + vec2(0.0, step2.y), lod).rgb);
            float m = max(dot(gu, gu), dot(gv, gv));
            if (m > 1e-20)
            {
                float inv = inversesqrt(m);
                // (textures are dark, 0..0.5: the slope per texel is scaled up to match)
                float k = u.extra.x * 6.0;
                vec3 bumped = N - k * ((hu - h0) * gu * inv + (hv - h0) * gv * inv) / max(exp2(lod * 0.5), 1.0);
                N = normalize(bumped);
            }
        }
        // cut-outs (alpha tested: leaves, fences) take their alpha from the full-size texture, so the averaged
        // alpha of small mip levels does not make them thin out and vanish in the distance
        float ta = pc.a.w > 0.5 ? textureLod(uTex, uvP, 0.0).a : t.a;
        rgb = t.rgb * 255.0 * vrgb / 128.0;
        A = ta * pc.b.x * vCol.a / 128.0;
    }
    else
    {
        rgb = vrgb;
        A = vCol.a;
    }
    A = floor(A + 0.001);
    if (pc.a.w > 0.5)
    {
        int m = int(pc.a.y + 0.5);
        float r = pc.a.z;
        bool pass = m == 1 || (m == 2 && A < r) || (m == 3 && A <= r) || (m == 4 && A == r) ||
                    (m == 5 && A >= r) || (m == 6 && A > r) || (m == 7 && A != r);
        if (!pass)
            discard;
    }
    // RAIN: solid level surfaces wet: darker, with sharp highlights, a little reflective; on flat ground, in
    // patches, puddles: darker still, flat as water, very reflective, rippled by the drops
    float wetRefl = 0.0, wetSpec = 0.0;
    if (u.extra3.z > 0.5 && hasN)
    {
        // WATER: small waves running across it (two sets of moving ripples bending the normal), catching the
        // moon and the torches; the reflection pass mirrors the scene in it
        float t = u.point.w;
        vec2 p = vWorld.xz;
        float e = 0.15;
        float h0 = noise2(p * 0.6 + vec2(t * 0.35, t * 0.2)) + 0.5 * noise2(p * 1.7 - vec2(t * 0.5, -t * 0.3));
        float hx = noise2((p + vec2(e, 0.0)) * 0.6 + vec2(t * 0.35, t * 0.2)) + 0.5 * noise2((p + vec2(e, 0.0)) * 1.7 - vec2(t * 0.5, -t * 0.3));
        float hz = noise2((p + vec2(0.0, e)) * 0.6 + vec2(t * 0.35, t * 0.2)) + 0.5 * noise2((p + vec2(0.0, e)) * 1.7 - vec2(t * 0.5, -t * 0.3));
        N = normalize(vec3(-(hx - h0) / e * 0.35, 1.0, -(hz - h0) / e * 0.35));
        wetSpec = 1.0;
        wetRefl = 0.75;
    }
    if (u.weather.x > 0.0 && hasN && u.extra.x > 0.0)
    {
        vec3 Ng = normalize(vNormal);
        float flatness = smoothstep(0.5, 0.92, Ng.y);
        float greenTex = smoothstep(0.004, 0.03, texel.g - max(texel.r, texel.b));
        float pn = noise2(vWorld.xz * 0.22) * 0.7 + noise2(vWorld.xz * 0.9 + 3.0) * 0.3;
        float puddle = smoothstep(0.6, 0.66, pn) * smoothstep(0.85, 0.97, Ng.y) * (1.0 - greenTex) * u.weather.y;
        float wet = u.weather.x * mix(0.55, 1.0, flatness);
        if (puddle > 0.0)
        {
            // the water lies flat over the stones' bumps; drops land and spread rings
            N = normalize(mix(N, Ng, puddle));
            vec2 rp = vWorld.xz * 1.6;
            vec2 rip = vec2(0.0);
            for (int k = 0; k < 2; ++k)
            {
                vec2 cell = floor(rp) + float(k) * 0.5;
                vec3 h = hash3(cell + float(k) * 17.0);
                float ph = fract(u.point.w * (0.9 + 0.6 * h.z) + h.x);
                vec2 c = cell + 0.2 + 0.6 * h.xy;
                vec2 dd = rp - c;
                float r = length(dd);
                float ring = sin((r - ph * 0.8) * 38.0) * smoothstep(0.08, 0.0, abs(r - ph * 0.8)) * (1.0 - ph);
                rip += dd / max(r, 1e-3) * ring;
            }
            N = normalize(N + vec3(rip.x, 0.0, rip.y) * 0.35 * puddle);
        }
        rgb *= mix(1.0, 0.88, wet) * mix(1.0, 0.82, puddle);
        {
            // WET SHEEN: rain-soaked stone and wood catch the night sky at grazing angles, and on walls rain water
            // runs down in thin moving trickles
            vec3 Vw = normalize(u.cam.xyz - vWorld);
            float fres = pow(1.0 - max(dot(N, Vw), 0.0), 4.0);
            rgb += vec3(40.0, 46.0, 60.0) * fres * wet * (1.0 - greenTex);
            if (abs(Ng.y) < 0.5)
            {
                vec2 acr = normalize(vec2(-Ng.z, Ng.x) + vec2(1e-5));
                float along = dot(vWorld.xz, acr);
                float lane = smoothstep(0.62, 0.8, noise2(vec2(along * 3.5, 0.0)));
                float run = smoothstep(0.55, 0.9, noise2(vec2(along * 3.5, vWorld.y * 0.9 + u.point.w * 1.6)));
                rgb += vec3(26.0, 30.0, 36.0) * lane * run * wet;
            }
        }
        wetRefl = puddle * 0.7; // (only puddles reflect: reflections on every wet floor cost too much)
        wetSpec = wet * 0.5 + puddle;
    }
    if (u.extra2.x > 0.0 && hasN && abs(N.y) < 0.6)
    {
        // GRIME: dark streaks running down walls where rain water has run, and dirt splashed up near the ground
        vec2 across = normalize(vec2(-N.z, N.x) + vec2(1e-5));
        float along = dot(vWorld.xz, across);
        float streak = noise2(vec2(along * 1.7, vWorld.y * 0.12)) * 0.6 + noise2(vec2(along * 4.1, vWorld.y * 0.3)) * 0.4;
        float s = smoothstep(0.55, 0.85, streak);
        float foot = u.extra2.w > 0.5 ? 1.0 - smoothstep(0.0, 2.5, vWorld.y - u.extra2.y) : 0.0;
        float g = clamp(s * 0.45 + foot * 0.45, 0.0, 0.7) * u.extra2.x;
        rgb = mix(rgb, rgb * vec3(0.92, 0.97, 0.8), g * 0.5) * (1.0 - g * 0.6);
    }
    if (u.extra.z > 0.0 && hasN)
    {
        // CRACKS AND STAINS: grey stone (tombs, walls, steps) gets fine cracks in patches and darker blotches,
        // in the surface's own plane (walls: across and up; tops: the ground plane)
        float mxc = max(texel.r, max(texel.g, texel.b)), mnc = min(texel.r, min(texel.g, texel.b));
        float stone = 1.0 - smoothstep(0.15, 0.35, (mxc - mnc) / max(mxc, 1e-3));
        if (stone > 0.0)
        {
            vec3 Ng = normalize(vNormal);
            vec2 q;
            if (abs(Ng.y) < 0.6)
            {
                vec2 acr = normalize(vec2(-Ng.z, Ng.x) + vec2(1e-5));
                q = vec2(dot(vWorld.xz, acr), vWorld.y);
            }
            else
                q = vWorld.xz;
            float patchC = smoothstep(0.5, 0.75, noise2(q * 0.18 + 3.1));
            float edge = cellEdge(q * 0.9 + vec2(noise2(q * 2.0), noise2(q * 2.0 + 5.0)) * 0.25);
            float crack = (1.0 - smoothstep(0.0, 0.05, edge)) * patchC;
            float stain = smoothstep(0.6, 0.85, noise2(q * 0.22 + 11.0) * 0.7 + noise2(q * 0.9) * 0.3);
            float k = u.extra.z * stone;
            rgb *= 1.0 - 0.55 * crack * k;
            rgb *= mix(vec3(1.0), vec3(0.78, 0.82, 0.7), stain * 0.6 * k);
        }
    }
    if (u.extra.y > 0.0 && hasN && N.y > 0.35)
    {
        // MOSS: on what faces up on stone (the tops of rocks, tombstones, ledges), in soft patches, thicker the
        // flatter it is, over the stone's own detail (its brightness kept)
        float patches = noise2(vWorld.xz * 0.35) * 0.65 + noise2(vWorld.xz * 1.3 + 7.0) * 0.35;
        float m = smoothstep(0.35, 0.8, N.y) * smoothstep(0.45, 0.7, patches) * u.extra.y;
        float l = lum(rgb);
        vec3 moss = vec3(l * 0.62, l * 1.05, l * 0.42) * (0.85 + 0.3 * noise2(vWorld.xz * 4.0));
        rgb = mix(rgb, moss, clamp(m, 0.0, 0.85));
    }
    if (u.flags.z > 0.5)
    {
        // SUN: brighter where a surface faces the light and nothing is in the way, darker on the far side and in
        // the shadows (ambient + strength * N.L * shadow)
        float ndl = hasN ? max(-dot(N, u.sun.xyz), 0.0) : 0.5;
        float sh = (pc.b.y > 0.5 && ndl > 0.0) ? lit() : 1.0;
        rgb *= u.flags.w + u.cam.w * ndl * sh;
    }
    else if (pc.b.y > 0.5)
    {
        // only surfaces facing the light take the new shadows: the level's walls already have their shading
        // baked in, and a wall seen edge-on by the light would only show acne
        float facing = hasN ? smoothstep(0.25, 0.6, -dot(N, pc.c.xyz)) : 0.0;
        if (facing > 0.0)
            rgb *= mix(1.0 - pc.b.z * facing, 1.0, lit());
    }
    if (u.point.x > 0.5)
    {
        // POINT LIGHTS: fire and glows brighten what is near them (smooth falloff, softly towards their side)
        vec3 add = vec3(0.0);
        vec3 n = N;
        int count = int(u.point.x + 0.5);
        for (int i = 0; i < 16; ++i)
        {
            if (i >= count)
                break;
            // FLICKER: fire breathes (three uneven waves, so it never repeats), each light at its own pace, and
            // its light sways a little with the flames, so the lit side of what is near it shifts
            float ph = dot(u.pointPos[i].xyz, vec3(0.131, 0.071, 0.113));
            float sp = u.pointCol[i].w;
            float tt = u.point.w * sp;
            float fk = sp > 0.0 ? u.weather.z : 0.0;
            vec3 sway = vec3(sin(tt * 0.61 + ph), 0.4 * sin(tt * 0.83 + ph * 2.1), cos(tt * 0.57 + ph * 1.3)) *
                        (sp > 5.0 ? 0.35 : 0.12) * fk;
            vec3 d = u.pointPos[i].xyz + sway - vWorld;
            float dist = length(d);
            float att = clamp(1.0 - dist / u.pointPos[i].w, 0.0, 1.0);
            att *= att;
            float ndl = hasN ? 0.35 + 0.65 * max(dot(n, d / max(dist, 1e-3)), 0.0) : 1.0;
            float wave = 0.5 * sin(tt + ph) + 0.3 * sin(tt * 2.37 + ph * 1.7) + 0.2 * sin(tt * 5.13 + ph * 2.9);
            float fl = 1.0 - 0.06 * fk + wave * 0.12 * fk;
            add += u.pointCol[i].rgb * att * ndl * fl;
        }
        // brighter near the lights, but never more than twice (bright surfaces next to a light burnt out)
        vec3 boost = add * u.point.y;
        rgb *= vec3(1.0) + vec3(1.0) - exp(-boost);
    }
    rgb *= u.point.z;
    if (u.weather.w > 0.0 && hasN)
    {
        // MOONLIT RIM: characters' edges catch a cold light, brightest on the side towards the moon, so Maximo and
        // the enemies stand out from the dark scenery
        vec3 V = normalize(u.cam.xyz - vWorld);
        float rim = pow(1.0 - max(dot(N, V), 0.0), 3.0);
        float side = 0.35 + 0.65 * max(dot(N, u.moon.xyz), 0.0);
        rgb += vec3(120.0, 140.0, 185.0) * rim * side * u.weather.w;
    }
    float refl = max(u.extra3.z, wetRefl);
    if (wetSpec > 0.0)
    {
        // the wet shine: a sharp highlight from the moon and the torches
        vec3 V = normalize(u.cam.xyz - vWorld);
        vec3 h = normalize(V - u.sun.xyz);
        float spec = pow(max(dot(N, h), 0.0), 90.0) * 0.6;
        int count = wetRefl > 0.0 ? int(u.point.x + 0.5) : 0; // (torch glints only in puddles)
        vec3 ps = vec3(0.0);
        // PUDDLE REFLECTIONS: in still water the torches show as long bright streaks stretched towards the camera
        // (as on a wet street at night), seen from well beyond their own light, and the moon as a bright spot
        vec3 T = normalize(cross(N, V) + vec3(1e-5));
        vec3 B = cross(T, N);
        float still = smoothstep(0.25, 0.6, wetRefl);
        for (int i = 0; i < 16; ++i)
        {
            if (i >= count)
                break;
            vec3 d = u.pointPos[i].xyz - vWorld;
            float dist = length(d);
            vec3 hl = normalize(V + d / max(dist, 1e-3));
            float glint = pow(max(dot(N, hl), 0.0), 60.0);
            float att = clamp(1.0 - dist / u.pointPos[i].w, 0.0, 1.0);
            float ht = dot(hl, T) / 0.05, hb = dot(hl, B) / 0.3;
            float streak = exp(-(ht * ht + hb * hb)) * clamp(1.0 - dist / (u.pointPos[i].w * 3.0), 0.0, 1.0);
            ps += u.pointCol[i].rgb * (att * glint * (1.0 - still) + streak * still * 1.6);
        }
        vec3 R = reflect(-V, N);
        float moonGlint = pow(max(dot(R, u.moon.xyz), 0.0), 400.0) * still;
        rgb += (vec3(spec * 200.0) + ps * 180.0 * u.point.y + vec3(230.0, 235.0, 255.0) * moonGlint) * wetSpec;
    }
    if (u.extra3.x > 0.0 && hasN)
    {
        // METAL: grey, light parts of the texture (iron, steel, silver armour) shine: a sharp highlight from the
        // moon and from every torch nearby, and the night sky reflected in them (dark below, blue-grey above,
        // the moon a bright spot); the metal's own colour darker under it, as real metal is
        float mx = max(texel.r, max(texel.g, texel.b)), mn = min(texel.r, min(texel.g, texel.b));
        float metal = smoothstep(0.18, 0.35, mx) * (1.0 - smoothstep(0.12, 0.3, (mx - mn) / max(mx, 1e-3))) * u.extra3.x;
        if (metal > 0.0)
        {
            vec3 V = normalize(u.cam.xyz - vWorld);
            vec3 R = reflect(-V, N);
            vec3 env = mix(vec3(14.0, 15.0, 18.0), vec3(70.0, 78.0, 95.0), smoothstep(-0.2, 0.8, R.y));
            env += vec3(255.0) * pow(max(dot(R, u.moon.xyz), 0.0), 120.0) * 0.8;
            vec3 h = normalize(V - u.sun.xyz);
            float spec = pow(max(dot(N, h), 0.0), 48.0) * 0.9;
            vec3 pointSpec = vec3(0.0);
            int count = int(u.point.x + 0.5);
            for (int i = 0; i < 16; ++i)
            {
                if (i >= count)
                    break;
                vec3 d = u.pointPos[i].xyz - vWorld;
                float dist = length(d);
                float att = clamp(1.0 - dist / u.pointPos[i].w, 0.0, 1.0);
                vec3 hl = normalize(V + d / max(dist, 1e-3));
                pointSpec += u.pointCol[i].rgb * att * att * pow(max(dot(N, hl), 0.0), 40.0);
            }
            float fres = 0.5 + 0.5 * pow(1.0 - max(dot(N, V), 0.0), 3.0);
            vec3 shine = (env * fres + vec3(255.0) * spec + vec3(255.0) * pointSpec * u.point.y) * (0.5 + mx);
            rgb = mix(rgb, rgb * 0.92 + shine * 0.4, metal);
            refl = max(refl, metal * 0.35);
        }
    }
    if (u.extra3.y > 0.0)
    {
        // WINDOWS AND CANDLES: the warm, bright parts of the texture give off their own light (not darkened by
        // the night), bright enough for the glow pass to put a halo around them
        float warm = smoothstep(0.5, 0.65, texel.r) * smoothstep(0.5, 0.6, texel.g / max(texel.r, 1e-3)) *
                     (1.0 - smoothstep(0.55, 0.7, texel.b / max(texel.r, 1e-3)));
        rgb = mix(rgb, texel * 255.0 * vec3(2.2, 1.9, 1.3), warm * u.extra3.y);
    }
    if (u.flags.y > 0.0 && hasN)
    {
        // highlights from the sun (Blinn-Phong), dimmed where the new shadows fall
        vec3 n = N;
        vec3 v = normalize(u.cam.xyz - vWorld);
        vec3 h = normalize(v - u.sun.xyz);
        float s = pow(max(dot(n, h), 0.0), 64.0) * u.flags.y * (pc.b.y > 0.5 ? lit() : 1.0);
        rgb += vec3(255.0) * s;
    }
    {
        int lm = int(u.look.x + 0.5);
        float tm = u.point.w;
        if (lm == 3)
        {
            // THE SOUL ORB, alive: its colours swirl slowly round the middle and it glows from inside
            vec2 c = fract(vUV) - 0.5;
            float r = length(c), an = atan(c.y, c.x);
            float swirl = 0.5 + 0.5 * sin(an * 3.0 + r * 14.0 - tm * 1.6);
            float inner = 1.0 - smoothstep(0.0, 0.45, r);
            rgb += texel * 255.0 * (0.18 + 0.3 * swirl * inner) + texel * 255.0 * inner * 0.25 * (0.5 + 0.5 * sin(tm * 2.0));
        }
        else if (lm == 4)
        {
            // GOLD FRAMES: now and then a gleam runs across them
            vec2 sp = gl_FragCoord.xy / 900.0;
            float band = fract((sp.x + sp.y) * 0.8 - tm * 0.25);
            float g = smoothstep(0.0, 0.03, band) * (1.0 - smoothstep(0.03, 0.08, band));
            rgb += texel * 255.0 * g * 1.4;
        }
        else if (lm >= 5 && pc.a.x > 0.5)
        {
            // CHARACTERS' MATERIALS, told from the texture's colour: skin warm and soft (a little light through
            // it in the shade), cloth matte, pale bone with a faint sheen; then the enemies' glowing eyes, and
            // ghosts lit from inside
            float mx = max(texel.r, max(texel.g, texel.b)), mn = min(texel.r, min(texel.g, texel.b));
            float sat = (mx - mn) / max(mx, 1e-3);
            float skin = smoothstep(0.12, 0.2, mx) * step(texel.b, texel.g) * step(texel.g, texel.r) *
                         smoothstep(0.15, 0.3, sat) * (1.0 - smoothstep(0.55, 0.75, sat));
            float bone = smoothstep(0.25, 0.4, mx) * (1.0 - smoothstep(0.1, 0.25, sat)) * step(texel.b, texel.r);
            float shade = 1.0 - clamp(lum(rgb) / max(lum(texel * 255.0), 1.0), 0.0, 1.0);
            rgb += texel * 255.0 * vec3(0.35, 0.12, 0.06) * skin * shade;
            rgb *= mix(vec3(1.0), vec3(1.04, 0.99, 0.95), skin);
            if (hasN)
            {
                vec3 Vc = normalize(u.cam.xyz - vWorld);
                float fr = pow(1.0 - max(dot(N, Vc), 0.0), 3.0);
                rgb += vec3(60.0, 58.0, 52.0) * fr * bone;
            }
            if (false && lm == 6 && vWorld.y > u.look.y && lum(texel) < 0.05) // (glowing eyes: removed)
            {
                int ec = int(u.look.z + 0.5);
                vec3 eye = ec == 0 ? vec3(255.0, 60.0, 30.0) : (ec == 1 ? vec3(90.0, 255.0, 80.0) : vec3(80.0, 220.0, 255.0));
                float pulse = 0.8 + 0.2 * sin(tm * 3.0 + u.look.y);
                rgb = mix(rgb, eye * 1.5 * pulse, 0.85);
            }
            if (lm == 7)
                rgb += texel * 255.0 * vec3(0.35, 0.55, 0.75) + vec3(8.0, 16.0, 28.0);
        }
    }
    // (with the reflections on, the main picture's alpha carries how reflective each pixel is: nothing in this
    // renderer blends with the picture's own alpha)
    oColor = vec4(clamp(rgb / 255.0, 0.0, 1.0), u.extra3.w > 0.5 ? clamp(refl, 0.0, 1.0) : clamp(A / 128.0, 0.0, 1.0));
}
