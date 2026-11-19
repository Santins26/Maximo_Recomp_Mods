#version 450
// NATIVE RENDERER, real-time shadows: the casters from the light (world -> shadow map).
layout(location = 1) in vec2 aUV;
layout(location = 3) in vec3 aWorld;
layout(location = 0) out vec2 vUV;
layout(push_constant) uniform PC { vec4 a; vec4 b; vec4 c; mat4 light; } pc;
void main()
{
    gl_Position = pc.light * vec4(aWorld, 1.0);
    vUV = aUV;
}
