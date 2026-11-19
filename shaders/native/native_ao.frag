#version 450
// NATIVE RENDERER, ambient occlusion from the native depth buffer (reversed: 1/w, 0 where nothing was drawn).
// The view-space position comes back from the game's screen matrix; the result multiplies the picture.
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 oColor;
layout(set = 0, binding = 0) uniform sampler2D uDepth;
// a: 1/width, 1/height, screen x scale, screen y scale.  b: radius (world units), strength, half width, half height
layout(push_constant) uniform PC { vec4 a; vec4 b; } pc;

vec3 viewPos(vec2 uv, float d)
{
    float w = 1.0 / d;
    vec2 ndc = uv * 2.0 - 1.0;
    return vec3(ndc.x * pc.b.z * w / pc.a.z, ndc.y * pc.b.w * w / pc.a.w, w);
}

vec2 project(vec3 p)
{
    vec2 ndc = vec2(p.x * pc.a.z / (p.z * pc.b.z), p.y * pc.a.w / (p.z * pc.b.w));
    return ndc * 0.5 + 0.5;
}

float hash(vec2 p)
{
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main()
{
    float d = texture(uDepth, vUV).r;
    if (d <= 1e-6)
    {
        oColor = vec4(1.0);
        return;
    }
    vec3 P = viewPos(vUV, d);
    // the normal from the neighbours on whichever side is closer in depth (no false creases along silhouettes)
    vec2 tx = vec2(pc.a.x, 0.0), ty = vec2(0.0, pc.a.y);
    float dl = texture(uDepth, vUV - tx).r, dr = texture(uDepth, vUV + tx).r;
    float du = texture(uDepth, vUV - ty).r, dd = texture(uDepth, vUV + ty).r;
    vec3 Pl = viewPos(vUV - tx, max(dl, 1e-6)), Pr = viewPos(vUV + tx, max(dr, 1e-6));
    vec3 Pu = viewPos(vUV - ty, max(du, 1e-6)), Pd = viewPos(vUV + ty, max(dd, 1e-6));
    vec3 ex = (abs(Pr.z - P.z) < abs(P.z - Pl.z)) ? Pr - P : P - Pl;
    vec3 ey = (abs(Pd.z - P.z) < abs(P.z - Pu.z)) ? Pd - P : P - Pu;
    vec3 N = normalize(cross(ex, ey));
    if (dot(N, P) > 0.0)
        N = -N; // face the camera
    float R = pc.b.x;
    // a rotated spiral of 12 taps over the hemisphere around N
    float angle0 = hash(gl_FragCoord.xy) * 6.2831853;
    vec3 T = normalize(abs(N.y) < 0.9 ? cross(N, vec3(0, 1, 0)) : cross(N, vec3(1, 0, 0)));
    vec3 B = cross(N, T);
    float occ = 0.0;
    const int TAPS = 12;
    for (int i = 0; i < TAPS; ++i)
    {
        float f = (float(i) + 0.5) / float(TAPS);
        float ang = angle0 + float(i) * 2.399963; // golden angle
        float r = sqrt(f);
        vec3 dir = T * (cos(ang) * r) + B * (sin(ang) * r) + N * sqrt(max(1.0 - f, 0.0));
        vec3 S = P + dir * (R * mix(0.2, 1.0, f));
        if (S.z <= 1.0)
            continue;
        vec2 uv = project(S);
        if (any(lessThan(uv, vec2(0.0))) || any(greaterThan(uv, vec2(1.0))))
            continue;
        float ds = texture(uDepth, uv).r;
        if (ds <= 1e-6)
            continue;
        float ws = 1.0 / ds;
        // the scene is in front of the tap point: occluded, fading out for things much nearer (another object)
        float diff = S.z - ws;
        if (diff > 0.02 * R)
            occ += clamp(1.0 - (diff - R) / R, 0.0, 1.0);
    }
    float ao = 1.0 - pc.b.y * occ / float(TAPS);
    oColor = vec4(vec3(clamp(ao, 0.0, 1.0)), 1.0);
}
