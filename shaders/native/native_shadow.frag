#version 450
// NATIVE RENDERER, real-time shadows: cut-out textures (leaves, fences) cast their shape.
layout(location = 0) in vec2 vUV;
layout(set = 0, binding = 0) uniform sampler2D uTex;
layout(push_constant) uniform PC { vec4 a; vec4 b; vec4 c; mat4 light; } pc;
void main()
{
    if (pc.a.x > 0.5 && pc.a.w > 0.5 && texture(uTex, vUV).a * pc.b.x < max(pc.a.z, 32.0))
        discard;
}
