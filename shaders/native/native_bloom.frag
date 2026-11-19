#version 450
// NATIVE RENDERER, bloom: bright parts of the picture blurred into a soft glow (quarter size), added back on top.
// mode 0: bright pass + horizontal blur, 1: vertical blur, 2: composite (the glow, scaled).
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 oColor;
layout(set = 0, binding = 0) uniform sampler2D uSrc;
// a: texel x, texel y, mode, threshold.  b: intensity
layout(push_constant) uniform PC { vec4 a; vec4 b; } pc;

vec3 bright(vec3 c)
{
    float m = dot(c, vec3(0.299, 0.587, 0.114)); // brightness as the eye sees it (red lava glows less than fire)
    return c * smoothstep(pc.a.w, pc.a.w + 0.25, m);
}

void main()
{
    const float w[5] = float[](0.2270270, 0.1945946, 0.1216216, 0.0540540, 0.0162162);
    int mode = int(pc.a.z + 0.5);
    if (mode == 2)
    {
        oColor = vec4(texture(uSrc, vUV).rgb * pc.b.x, 0.0);
        return;
    }
    vec2 dir = mode == 0 ? vec2(pc.a.x, 0.0) : vec2(0.0, pc.a.y);
    vec3 s = vec3(0.0);
    for (int i = -4; i <= 4; ++i)
    {
        vec3 c = texture(uSrc, vUV + dir * float(i) * 1.5).rgb;
        s += (mode == 0 ? bright(c) : c) * w[abs(i)];
    }
    oColor = vec4(s, 1.0);
}
