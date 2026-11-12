#version 450
layout(location = 0) in vec2 Position;
layout(location = 0) out vec2 vUV;
layout(push_constant, std430) uniform Push { vec4 texel; vec4 params; } push;
void main()
{
    gl_Position = vec4(Position, 0.0, 1.0);
    vUV = Position * 0.5 + 0.5;
}
