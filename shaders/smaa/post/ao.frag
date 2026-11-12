#version 450
// Scene ambient occlusion from the scene depth the Maximo ubershader records (one Z per 32-bit colour word, at the
// colour's VRAM word index), read with the PSMCT32 swizzle of the displayed buffer, at the single-sampled size.
// Output: R = visibility (1 = open, lower = occluded).
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 FragColor;
layout(set = 0, binding = 0, std430) readonly buffer VRAM { uint words[]; } vram;
// zinfo: x = DISPFB.FBP * 2048 (word base), y = FBW (64-pixel units), z unused, w = word mask (VRAM words - 1)
// area:  x, y = origin of the displayed buffer in GS pixels, z, w = single-sampled width/height
layout(push_constant, std430) uniform Push { uvec4 zinfo; vec4 area; vec4 params; } push;

const int kBlock32[32] = int[](0, 1, 4, 5, 16, 17, 20, 21, 2, 3, 6, 7, 18, 19, 22, 23,
                               8, 9, 12, 13, 24, 25, 28, 29, 10, 11, 14, 15, 26, 27, 30, 31);
const int kColumn32[64] = int[](0, 1, 4, 5, 8, 9, 12, 13, 2, 3, 6, 7, 10, 11, 14, 15,
                                16, 17, 20, 21, 24, 25, 28, 29, 18, 19, 22, 23, 26, 27, 30, 31,
                                32, 33, 36, 37, 40, 41, 44, 45, 34, 35, 38, 39, 42, 43, 46, 47,
                                48, 49, 52, 53, 56, 57, 60, 61, 50, 51, 54, 55, 58, 59, 62, 63);

float depthAt(ivec2 local)
{
    ivec2 size = ivec2(push.area.zw);
    local = clamp(local, ivec2(0), size - 1);
    ivec2 p = local + ivec2(push.area.xy);
    uint page = uint(p.y >> 5) * push.zinfo.y + uint(p.x >> 6);
    uint block = uint(kBlock32[((p.y >> 3) & 3) * 8 + ((p.x >> 3) & 7)]);
    uint column = uint(kColumn32[(p.y & 7) * 8 + (p.x & 7)]);
    uint addr = (push.zinfo.x + page * 2048u + block * 64u + column) & push.zinfo.w;
    uint z = vram.words[addr];
    return float(z);
}

void main()
{
    ivec2 size = ivec2(push.area.zw);
    ivec2 px = ivec2(vUV * vec2(size));
    float z0 = depthAt(px);
    if (push.params.w > 0.5) // debug: the depth itself, log2 mapped between params.z (far) and params.z + 8 (near)
    {
        float v = z0 <= 0.0 ? 0.0 : clamp((log2(z0) - push.params.z) / 8.0, 0.0, 1.0);
        FragColor = vec4(vec3(v), 1.0);
        return;
    }
    if (z0 <= 0.0)
    {
        FragColor = vec4(1.0);
        return;
    }
    // GS Z grows towards the camera (about 1/w), so log2(z) differences are view-depth ratios whatever the scale.
    // A tap closer than the centre by 0.3% to 6% occludes, fading out above that so objects well in front of the
    // background (silhouettes) leave no dark halo. Two rings of 8 taps, rotated per pixel.
    float l0 = log2(z0);
    float rot = fract(sin(dot(vec2(px), vec2(12.9898, 78.233))) * 43758.5453) * 6.2831853;
    float occ = 0.0;
    for (int ring = 0; ring < 2; ++ring)
    {
        // Constant size in the world: GS Z ~ 1/w, so the screen radius grows with Z (2^19 is mid-distance here;
        // the game's Z runs from about 2^15, far, to 2^21.5, near).
        float radius = push.params.x * clamp(exp2((l0 - 19.0) * 0.8), 0.45, 3.0) * (ring == 0 ? 0.45 : 1.0);
        for (int i = 0; i < 8; ++i)
        {
            float a = rot + float(i) * 0.78539816 + float(ring) * 0.3927;
            ivec2 q = px + ivec2(round(vec2(cos(a), sin(a)) * radius));
            float zs = depthAt(q);
            if (zs <= 0.0)
                continue;
            float rel = log2(zs) - l0; // > 0: the tap is in front
            occ += smoothstep(0.003, 0.02, rel) * (1.0 - smoothstep(0.04, 0.08, rel));
        }
    }
    float vis = 1.0 - clamp(occ / 16.0 * push.params.y, 0.0, 0.6);
    FragColor = vec4(vis, vis, vis, 1.0);
}
