#version 450
// The picture darkened by the (lightly blurred) occlusion. params: x = strength, w = debug view (1 depth, 2 AO).
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 FragColor;
layout(set = 0, binding = 0) uniform sampler2D uSrc;
layout(set = 0, binding = 1) uniform sampler2D uAo;
layout(push_constant, std430) uniform Push { vec4 texel; vec4 params; } push;
void main()
{
    vec2 t = push.texel.zw; // AO texel size
    // 3x3 Gaussian over the occlusion (it is noisy per pixel: 16 rotated taps).
    float ao = 0.0;
    for (int y = -1; y <= 1; ++y)
        for (int x = -1; x <= 1; ++x)
            ao += texture(uAo, vUV + t * vec2(x, y) * 1.25).r * ((x == 0 ? 2.0 : 1.0) * (y == 0 ? 2.0 : 1.0));
    ao /= 16.0;
    if (push.params.w > 0.5)
    {
        FragColor = vec4(vec3(push.params.w > 1.5 ? ao : texture(uAo, vUV).r), 1.0);
        return;
    }
    vec3 c = texture(uSrc, vUV).rgb;
    c *= mix(1.0, ao, push.params.x);
    FragColor = vec4(c, 1.0);
}
