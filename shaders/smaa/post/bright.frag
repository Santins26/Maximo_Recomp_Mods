#version 450
// Quarter-size bright pass: 4 bilinear taps cover the 4x4 source block; only what is brighter than params.x glows.
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 FragColor;
layout(set = 0, binding = 0) uniform sampler2D uSrc;
layout(push_constant, std430) uniform Push { vec4 texel; vec4 params; } push;
void main()
{
    vec2 t = push.texel.xy;
    vec3 c = texture(uSrc, vUV + t * vec2(-1.0, -1.0)).rgb + texture(uSrc, vUV + t * vec2(1.0, -1.0)).rgb +
             texture(uSrc, vUV + t * vec2(-1.0, 1.0)).rgb + texture(uSrc, vUV + t * vec2(1.0, 1.0)).rgb;
    c *= 0.25;
    c *= c; // to linear (gamma 2)
    float lum = dot(c, vec3(0.2126, 0.7152, 0.0722));
    float k = max(lum - push.params.x, 0.0) / max(lum, 1e-4);
    FragColor = vec4(c * k, 1.0);
}
