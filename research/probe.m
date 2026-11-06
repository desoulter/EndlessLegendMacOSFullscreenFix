#import <AppKit/AppKit.h>
#import <OpenGL/OpenGL.h>
#import <OpenGL/gl.h>
#import <OpenGL/glext.h>

static NSWindow *window;
static NSView *view;
static NSOpenGLContext *wrapper;
static CGLContextObj master, context;
static CGLPixelFormatObj pixel;
static NSDictionary *config;
static FILE *report;
static int frames, phase;
static NSTimeInterval started;
static void Log(NSString *message) {
    fprintf(report, "%.3f %s\n", [NSDate timeIntervalSinceReferenceDate]-started, message.UTF8String);
    fflush(report);
}
static void Attach(BOOL update) {
    wrapper = nil;
    wrapper = [[NSOpenGLContext alloc] initWithCGLContextObj:context];
    [wrapper setView:view];
    if(update) [wrapper update];
    CGLSetCurrentContext(context);
    GLint sync=1;CGLSetParameter(context,kCGLCPSwapInterval,&sync);
}
@interface Probe : NSObject <NSApplicationDelegate,NSWindowDelegate>
@end
@implementation Probe
-(void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSString *folder=@"/tmp/el-fullscreen-research";
    NSData *data=[NSData dataWithContentsOfFile:[folder stringByAppendingPathComponent:@"probe-config.json"]];
    config=[NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] ?: @{};
    NSString *mode=config[@"mode"] ?: @"baseline";
    report=fopen([[folder stringByAppendingPathComponent:[NSString stringWithFormat:@"probe-%@.log",mode]] fileSystemRepresentation],"w");
    started=[NSDate timeIntervalSinceReferenceDate];
    Log([NSString stringWithFormat:@"starting %@",mode]);
    CGLPixelFormatAttribute attributes[]={kCGLPFADoubleBuffer,kCGLPFAAccelerated,kCGLPFAColorSize,(CGLPixelFormatAttribute)32,kCGLPFAAlphaSize,(CGLPixelFormatAttribute)8,kCGLPFADepthSize,(CGLPixelFormatAttribute)24,kCGLPFAStencilSize,(CGLPixelFormatAttribute)8,(CGLPixelFormatAttribute)0};
    GLint count=0;CGLError error=CGLChoosePixelFormat(attributes,&pixel,&count);
    if(error||!pixel){Log(@"pixel format failed");[NSApp terminate:nil];return;}
    CGLCreateContext(pixel,NULL,&master);CGLCreateContext(pixel,master,&context);
    window=[[NSWindow alloc] initWithContentRect:NSMakeRect(100,100,960,640) styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    window.title=[NSString stringWithFormat:@"EL OpenGL Probe — %@",mode];window.delegate=self;
    view=[[NSView alloc] initWithFrame:NSMakeRect(0,0,960,640)];window.contentView=view;
    [window makeKeyAndOrderFront:nil];[NSApp activateIgnoringOtherApps:YES];
    Attach(YES);
    Log([NSString stringWithFormat:@"GL %s renderer %s",glGetString(GL_VERSION),glGetString(GL_RENDERER)]);
    [NSTimer scheduledTimerWithTimeInterval:1.0/30 target:self selector:@selector(frame:) userInfo:nil repeats:YES];
}
-(void)windowDidResize:(NSNotification *)notification {
    if([config[@"update"] boolValue]){[wrapper update];Log(@"resize -> update");}
}
-(void)frame:(NSTimer *)timer {
    NSTimeInterval time=[NSDate timeIntervalSinceReferenceDate]-started;
    if(time>9){Log([NSString stringWithFormat:@"PASS frames=%d",frames]);[NSApp terminate:nil];return;}
    if(time>1.5&&phase==0){phase=1;Log(@"resize window");[window setContentSize:NSMakeSize(1180,760)];}
    if(time>3&&phase==1){
        phase=2;
        if([config[@"recreate"] boolValue]) {
            Log(@"recreate shared CGL context");
            CGLContextObj old=context;
            if([config[@"finish"] boolValue]){CGLSetCurrentContext(old);glFinish();Log(@"finish old context");}
            if([config[@"detach"] boolValue]){[wrapper clearDrawable];Log(@"detach old drawable before attaching new context");}
            CGLCreateContext(pixel,master,&context);Attach([config[@"update"] boolValue]);
            CGLSetCurrentContext(master);glFinish();if([config[@"release"] boolValue]){CGLReleaseContext(old);Log(@"release old CGL reference");}else{CGLDestroyContext(old);}CGLSetCurrentContext(context);
        }
        if([config[@"native_fullscreen"] boolValue]){Log(@"enter native fullscreen");[window toggleFullScreen:nil];}
    }
    CGLSetCurrentContext(context);
    NSRect size=view.bounds;
    glViewport(0,0,(GLsizei)size.size.width,(GLsizei)size.size.height);
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT,0);
    glReadBuffer(GL_BACK);glDrawBuffer(GL_BACK);
    glClearColor(0.1f,phase==0?0.25f:0.7f,0.35f,1.0f);glClear(GL_COLOR_BUFFER_BIT);
    GLenum glerror=glGetError();
    if(glerror){Log([NSString stringWithFormat:@"GL error %x",glerror]);}
    if(frames%30==0)Log([NSString stringWithFormat:@"present frame=%d phase=%d context=%p view=%p wrapper=%p size=%.0fx%.0f",frames,phase,context,view,wrapper,size.size.width,size.size.height]);
    CGLFlushDrawable(context);frames++;
}
@end
int main(int argc,char **argv){@autoreleasepool{
    NSApplication *application=[NSApplication sharedApplication];application.activationPolicy=NSApplicationActivationPolicyRegular;
    NSMenu *menu=[[NSMenu alloc] initWithTitle:@"Main"];NSMenuItem *entry=[[NSMenuItem alloc] initWithTitle:@"Probe" action:NULL keyEquivalent:@""];[menu addItem:entry];
    NSMenu *submenu=[[NSMenu alloc] initWithTitle:@"Probe"];[submenu addItemWithTitle:@"Quit" action:@selector(terminate:) keyEquivalent:@"q"];entry.submenu=submenu;application.mainMenu=menu;
    Probe *delegate=[Probe new];application.delegate=delegate;[application run];
}return 0;}
