#version 450
// Picture + bloom, then a light grade: saturation, S-curve, cool shadows / warm highlights, vignette.
// params = (bloom strength, saturation, curve amount, vignette)
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 FragColor;
layout(set = 0, binding = 0) uniform sampler2D uSrc;
layout(set = 0, binding = 1) uniform sampler2D uBloom;
layout(push_constant, std430) uniform Push { vec4 texel; vec4 params; } push;
void main()
{
    vec3 c = texture(uSrc, vUV).rgb;
    c *= c;
    c += texture(uBloom, vUV).rgb * push.params.x;
    c = sqrt(max(c, vec3(0.0)));
    float lum = dot(c, vec3(0.2126, 0.7152, 0.0722));
    c = mix(vec3(lum), c, push.params.y);
    float w = clamp((lum - 0.5) * 2.0, -1.0, 1.0) * 0.02;
    c += vec3(w, 0.0, -w);
    c = clamp(c, 0.0, 1.0);
    c = mix(c, c * c * (3.0 - 2.0 * c), push.params.z);
    vec2 v = vUV * 2.0 - 1.0;
    c *= 1.0 - push.params.w * dot(v, v) * 0.5;
    FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
