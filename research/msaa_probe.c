// Headless legacy-OpenGL reproduction of Unity's fullscreen MSAA allocation.
// clang -arch x86_64 -Wno-deprecated-declarations -framework OpenGL msaa_probe.c -o /tmp/el-msaa-probe
#include <OpenGL/OpenGL.h>
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <stdio.h>

static int check_allocation(int requested, int samples, int width, int height)
{
    GLuint fbo, color, depth;
    glGenFramebuffersEXT(1, &fbo);
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, fbo);
    glGenRenderbuffersEXT(1, &color);
    glBindRenderbufferEXT(GL_RENDERBUFFER_EXT, color);
    glRenderbufferStorageMultisampleEXT(GL_RENDERBUFFER_EXT, samples, GL_RGBA8, width, height);
    GLenum color_error = glGetError();
    GLint actual_width = 0, actual_samples = 0;
    glGetRenderbufferParameterivEXT(GL_RENDERBUFFER_EXT, GL_RENDERBUFFER_WIDTH_EXT, &actual_width);
    glGetRenderbufferParameterivEXT(GL_RENDERBUFFER_EXT, GL_RENDERBUFFER_SAMPLES_EXT, &actual_samples);
    glFramebufferRenderbufferEXT(GL_FRAMEBUFFER_EXT, GL_COLOR_ATTACHMENT0_EXT, GL_RENDERBUFFER_EXT, color);
    glGenRenderbuffersEXT(1, &depth);
    glBindRenderbufferEXT(GL_RENDERBUFFER_EXT, depth);
    glRenderbufferStorageMultisampleEXT(GL_RENDERBUFFER_EXT, samples, GL_DEPTH24_STENCIL8_EXT, width, height);
    GLenum depth_error = glGetError();
    glFramebufferRenderbufferEXT(GL_FRAMEBUFFER_EXT, GL_DEPTH_ATTACHMENT_EXT, GL_RENDERBUFFER_EXT, depth);
    glFramebufferRenderbufferEXT(GL_FRAMEBUFFER_EXT, GL_STENCIL_ATTACHMENT_EXT, GL_RENDERBUFFER_EXT, depth);
    GLenum status = glCheckFramebufferStatusEXT(GL_FRAMEBUFFER_EXT);
    int valid = color_error == GL_NO_ERROR && depth_error == GL_NO_ERROR &&
                status == GL_FRAMEBUFFER_COMPLETE_EXT && actual_width == width && actual_samples == samples;
    printf("%dx%d requested=%d allocated=%d width=%d samples=%d colorError=%x depthError=%x status=%x %s\n",
           width, height, requested, samples, actual_width, actual_samples, color_error, depth_error, status,
           valid ? "COMPLETE" : "REJECTED");
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, 0);
    glDeleteFramebuffersEXT(1, &fbo);
    glDeleteRenderbuffersEXT(1, &color);
    glDeleteRenderbuffersEXT(1, &depth);
    return valid;
}

int main(void)
{
    CGLPixelFormatAttribute attrs[] = {
        kCGLPFADoubleBuffer, kCGLPFAAccelerated,
        kCGLPFAColorSize, 32, kCGLPFAAlphaSize, 8,
        kCGLPFADepthSize, 24, kCGLPFAStencilSize, 8, 0
    };
    CGLPixelFormatObj pixel_format = NULL;
    CGLContextObj context = NULL;
    GLint count = 0;
    CGLError error = CGLChoosePixelFormat(attrs, &pixel_format, &count);
    if (error != kCGLNoError || !pixel_format) {
        fprintf(stderr, "CGLChoosePixelFormat: %s; matching formats=%d\n", CGLErrorString(error), count);
        return 2;
    }
    error = CGLCreateContext(pixel_format, NULL, &context);
    if (error != kCGLNoError) {
        fprintf(stderr, "CGLCreateContext: %s\n", CGLErrorString(error));
        CGLDestroyPixelFormat(pixel_format);
        return 2;
    }
    CGLDestroyPixelFormat(pixel_format);
    CGLSetCurrentContext(context);
    GLint maximum = 0;
    glGetIntegerv(GL_MAX_SAMPLES_EXT, &maximum);
    printf("renderer=%s GL_MAX_SAMPLES_EXT=%d\n", glGetString(GL_RENDERER), maximum);
    if (maximum <= 0) return 3;
    int failures = 0;
    const int sizes[][2] = {{2048, 1330}, {1920, 1200}, {1512, 945}};
    for (int i = 0; i < 3; i++) {
        // The unguarded request must fail; capping must produce a complete FBO.
        failures += check_allocation(maximum + 1, maximum + 1, sizes[i][0], sizes[i][1]);
        failures += !check_allocation(8, maximum < 8 ? maximum : 8, sizes[i][0], sizes[i][1]);
    }
    CGLSetCurrentContext(NULL);
    CGLDestroyContext(context);
    return failures ? 1 : 0;
}
