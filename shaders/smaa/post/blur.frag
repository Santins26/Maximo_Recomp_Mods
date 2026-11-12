#version 450
// 9-tap Gaussian along params.xy (in texels of this image), 5 fetches with linear filtering.
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 FragColor;
layout(set = 0, binding = 0) uniform sampler2D uSrc;
layout(push_constant, std430) uniform Push { vec4 texel; vec4 params; } push;
void main()
{
    vec2 d = push.texel.xy * push.params.xy;
    vec3 c = texture(uSrc, vUV).rgb * 0.2270270270;
    c += (texture(uSrc, vUV + d * 1.3846153846).rgb + texture(uSrc, vUV - d * 1.3846153846).rgb) * 0.3162162162;
    c += (texture(uSrc, vUV + d * 3.2307692308).rgb + texture(uSrc, vUV - d * 3.2307692308).rgb) * 0.0702702703;
    FragColor = vec4(c, 1.0);
}
