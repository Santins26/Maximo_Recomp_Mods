# Maximo: Ghosts to Glory (SLES-50703, SLUS-20017, SLPM-62127): game code recompiled from the player's disc + PS2Recomp runtime.
# build.cmd drives this in two steps: MAXIMO_TOOLS_ONLY builds the tools, which generate the game code; then the game.
cmake_minimum_required(VERSION 3.21)
project(maximo_boot LANGUAGES C CXX)
include(FetchContent)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
option(MAXIMO_TOOLS_ONLY "Build only the tools that generate the game code" OFF)
set(PS2RECOMP_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/external/PS2Recomp" CACHE PATH "PS2Recomp source tree")
set(GENERATED_DIR "${CMAKE_CURRENT_SOURCE_DIR}/generated/game" CACHE PATH "Game code generated from the player's disc")
set(MAXIMO_VU1AOT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/generated/vu1aot" CACHE PATH "VU1 code generated from the player's disc")
set(PS2X_BUILD_RECOMP ON CACHE BOOL "" FORCE)
set(PS2X_BUILD_ANALYZER OFF CACHE BOOL "" FORCE)
set(PS2X_BUILD_TEST OFF CACHE BOOL "" FORCE)
set(PS2X_BUILD_STUDIO OFF CACHE BOOL "" FORCE)
set(PS2X_ENABLE_DEBUG_UI OFF CACHE BOOL "" FORCE)
set(PS2X_ENABLE_FFMPEG OFF CACHE BOOL "" FORCE)
set(PS2X_ENABLE_RUNTIME_LOGS ON CACHE BOOL "" FORCE)
set(PS2X_ENABLE_AGRESSIVE_LOGS OFF CACHE BOOL "" FORCE)
set(PS2X_ENABLE_IOP_RPC_TRACE OFF CACHE BOOL "" FORCE)
set(PS2X_ENABLE_SCCACHE OFF CACHE BOOL "" FORCE)
# Release keeps full optimisation but also emits PDBs, so the sampling profiler (MAXIMO_PROFILE) can name functions.
add_compile_options($<$<CONFIG:Release>:/Zi> $<$<CONFIG:Release>:/FS>)
add_link_options($<$<CONFIG:Release>:/DEBUG> $<$<CONFIG:Release>:/OPT:REF> $<$<CONFIG:Release>:/OPT:ICF>)
add_subdirectory(${PS2RECOMP_ROOT} ps2recomp)

add_executable(maximo_kit tools/kit/maximo_kit.cpp)
target_link_libraries(maximo_kit PRIVATE bcrypt)
if(MAXIMO_TOOLS_ONLY)
    return()
endif()

file(GLOB GENERATED_SRC ${GENERATED_DIR}/*.cpp)
add_library(maximo_recompiled STATIC ${GENERATED_SRC})
target_include_directories(maximo_recompiled PUBLIC ${GENERATED_DIR})
target_link_libraries(maximo_recompiled PUBLIC ps2_runtime)
target_compile_options(maximo_recompiled PRIVATE /MP /bigobj /FS)
target_precompile_headers(maximo_recompiled PRIVATE
    ${PS2RECOMP_ROOT}/ps2xRuntime/include/ps2_runtime_macros.h
    ${PS2RECOMP_ROOT}/ps2xRuntime/include/ps2_runtime.h)

add_executable(maximo_boot maximo_boot.cpp)
target_link_libraries(maximo_boot PRIVATE maximo_recompiled dbghelp)
# Which release the generated code is from (build.cmd reads it off the disc): maximo_region.h maps the game addresses.
set(MAXIMO_REGION "PAL" CACHE STRING "Release of the player's disc: PAL, USA or JPN")
set_property(CACHE MAXIMO_REGION PROPERTY STRINGS PAL USA JPN)
if(NOT MAXIMO_REGION STREQUAL "PAL")
    target_compile_definitions(maximo_boot PRIVATE MAXIMO_REGION_${MAXIMO_REGION}=1)
endif()

# HD texture packs (HD_TEXTURES.md): `maximo_boot --hdtex-convert` turns PCSX2 replacement packs into pack.mhtx files.
# PNG decoding and scaling: stb (public domain, from raylib's tree); zip reading: miniz (MIT, from Granite/fossilize);
# hashing: xxHash 0.8.2 (BSD-2, vendor/xxhash-0.8.2); BC7: bc7enc (MIT or public domain, vendor/bc7enc).
set(MAXIMO_PARALLEL_GS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/external/parallel-gs" CACHE PATH "paraLLEl-GS source tree")
set(MAXIMO_MINIZ_DIR "${MAXIMO_PARALLEL_GS_DIR}/Granite/third_party/fossilize/miniz" CACHE PATH "miniz (miniz.c/.h)")
FetchContent_GetProperties(raylib)
set(MAXIMO_STB_DIR "${raylib_SOURCE_DIR}/src/external" CACHE PATH "stb_image.h, stb_image_resize2.h")
set(MAXIMO_XXHASH_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/xxhash" CACHE PATH "xxhash.h")
set(MAXIMO_BC7ENC_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/bc7enc" CACHE PATH "bc7enc.c/.h")
target_sources(maximo_boot PRIVATE hdtex/hdtex_convert.cpp ${MAXIMO_MINIZ_DIR}/miniz.c ${MAXIMO_BC7ENC_DIR}/bc7enc.c)
target_include_directories(maximo_boot PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/hdtex ${MAXIMO_MINIZ_DIR} ${MAXIMO_STB_DIR}
                           ${MAXIMO_XXHASH_DIR} ${MAXIMO_BC7ENC_DIR})

# Ahead-of-time compiled VU1 microprograms (ps2x_vu1aot output, generated from the player's disc).
file(GLOB VU1AOT_SRC CONFIGURE_DEPENDS ${MAXIMO_VU1AOT_DIR}/*.cpp)
add_library(maximo_vu1aot STATIC ${VU1AOT_SRC})
target_link_libraries(maximo_vu1aot PUBLIC ps2_runtime)
target_compile_options(maximo_vu1aot PRIVATE /bigobj /FS $<$<CONFIG:Release>:/O2 /Oi /fp:fast /arch:AVX2 /GS- /DNDEBUG>)
target_link_libraries(maximo_boot PRIVATE maximo_vu1aot)

# FFmpeg (an LGPL shared build, e.g. ffmpeg-n7.1-...-lgpl-amd64-shared) decodes the movie pictures (MAXIMO_MOVIE_FFMPEG,
# MOVIE_SPEED.md). The DLLs are delay-loaded and copied next to the exe; without them the game's own decoder runs.
set(MAXIMO_FFMPEG_DIR "" CACHE PATH "FFmpeg shared build (include/, bin/) for the movie decoder; empty: none")
if(MAXIMO_FFMPEG_DIR)
    target_include_directories(maximo_boot PRIVATE ${MAXIMO_FFMPEG_DIR}/include)
    target_link_libraries(maximo_boot PRIVATE ${MAXIMO_FFMPEG_DIR}/bin/avcodec.lib ${MAXIMO_FFMPEG_DIR}/bin/avutil.lib
                          delayimp)
    target_link_options(maximo_boot PRIVATE /DELAYLOAD:avcodec-61.dll /DELAYLOAD:avutil-59.dll)
    target_compile_definitions(maximo_boot PRIVATE MAXIMO_HAS_FFMPEG=1)
    # avcodec-61 and what it loads (its image codecs come along with this build).
    foreach(dll avcodec-61 avutil-59 swresample-5 libwebp libwebpmux libsharpyuv jxl jxl_cms jxl_threads brotlicommon
                brotlidec brotlienc zlib1)
        add_custom_command(TARGET maximo_boot POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different
                           ${MAXIMO_FFMPEG_DIR}/bin/${dll}.dll $<TARGET_FILE_DIR:maximo_boot>)
    endforeach()
endif()

option(MAXIMO_ENABLE_GPU_SHADOW "Build the paraLLEl-GS renderer" ON)
if(MAXIMO_ENABLE_GPU_SHADOW)
    set(PARALLEL_GS_STANDALONE ON CACHE BOOL "" FORCE)
    set(GRANITE_TOOLS OFF CACHE BOOL "" FORCE)
    set(GRANITE_INSTALL_TARGETS OFF CACHE BOOL "" FORCE)
    set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreadedDLL CACHE STRING "" FORCE)
    add_subdirectory(${MAXIMO_PARALLEL_GS_DIR} parallel-gs)
    set(CMAKE_CXX_STANDARD 20)
    target_sources(maximo_boot PRIVATE gpu_shadow.cpp)
    target_compile_definitions(maximo_boot PRIVATE MAXIMO_ENABLE_GPU_SHADOW=1)
    target_link_libraries(maximo_boot PRIVATE parallel-gs)
    # MAXIMO_SMAA: SPIR-V in shaders/smaa/smaa_spv.h; the area/search lookup tables come from Granite (MIT).
    target_include_directories(maximo_boot PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/shaders/smaa
                               ${MAXIMO_PARALLEL_GS_DIR}/Granite/tools/smaa)
    # HD texture replacement at run time (MAXIMO_HDTEX_PACKS).
    target_sources(maximo_boot PRIVATE hdtex/hdtex_runtime.cpp)
endif()
