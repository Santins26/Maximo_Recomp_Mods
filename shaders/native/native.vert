#version 450
// NATIVE RENDERER: vertices arrive already in clip space (transformed on the CPU from the game's own matrices),
// with their world position for the real-time shadows.
layout(location = 0) in vec4 aPos;
layout(location = 1) in vec2 aUV;
layout(location = 2) in vec4 aCol;
layout(location = 3) in vec3 aWorld;
layout(location = 4) in vec3 aNormal;
layout(location = 5) in vec3 aRaw;
layout(location = 6) in vec3 aMNormal;
layout(location = 0) out vec2 vUV;
layout(location = 1) out vec4 vCol;
layout(location = 2) out vec3 vWorld;
layout(location = 3) out vec3 vNormal;
layout(location = 4) out vec3 vRaw;
layout(location = 5) out vec3 vMNormal;
layout(push_constant) uniform PC { vec4 a; vec4 b; vec4 c; mat4 light; } pc;
void main()
{
    gl_Position = aPos;
    vUV = aUV;
    vCol = aCol;
    vWorld = aWorld;
    vNormal = aNormal;
    vRaw = aRaw;
    vMNormal = aMNormal;
}
