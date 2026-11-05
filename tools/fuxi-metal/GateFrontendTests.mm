/* Test policy injection only. Production always reads the protected file. */
#include "ClientGate.hpp"
#include "BackboardGate.hpp"
#include <cassert>
#include <atomic>
#include <cstdlib>
#include <string>
static unsigned policyReads;
static fuxi::ClientMode testPolicy(const char *,const char *process) {
 ++policyReads;
 if(!strcmp(process,"gate-test"))return getenv("FUXI_TEST_GATE_ALLOW")?fuxi::ClientMode::allow:fuxi::ClientMode::deny;
 std::string data=getenv("FUXI_TEST_GATE_CONFIG")?getenv("FUXI_TEST_GATE_CONFIG"):"";size_t n=data.size();data.push_back(0);
 return fuxi::parseClients(data.data(),n,process,getenv("FUXI_TEST_BOOTARGS"));
}
static bool testAdmission(){const char *dir=getenv("FUXI_TEST_GATE_DIR");assert(dir);
 return fuxi::backboardGate.admit("aaaaaaaa-1111-2222-3333-444444444444",open(dir,O_RDONLY|O_DIRECTORY|O_NOFOLLOW));}
static unsigned preferenceWrites,setupEnvironments;
static void testPreference(const char *key){
 assert(!strcmp(key,"CAEnableDeepFramebuffer") || !strcmp(key,"CAEnableFramebufferCompression") || !strcmp(key,"CAEnableIOSurfaceCompression"));++preferenceWrites;
}
static int testEnvironment(const char *key,const char *value,int overwrite){
 if(!strcmp(key,"TU_DEBUG")){assert(!strcmp(value,"sysmem,nocb") && !overwrite);}
 else {assert(!strcmp(key,"CA_DISABLE_FRAMEBUFFER_COMPRESSION") && !strcmp(value,"1") && overwrite);}
 ++setupEnvironments;return 0;
}
#define FUXI_MTL_SET_CA_PREFERENCE(key) testPreference(key)
#define FUXI_MTL_SET_ENV(key,value,overwrite) testEnvironment(key,value,overwrite)
#define FUXI_MTL_READ_CLIENTS testPolicy
#define FUXI_MTL_BOOTARGS(out,size) (snprintf(out,size,"%s",getenv("FUXI_TEST_BOOTARGS")?:""),true)
#define FUXI_MTL_BACKBOARD_ADMIT() testAdmission()
#define FUXI_MTL_BACKBOARD_MODE() fuxi::backboardMode(getenv("FUXI_TEST_BOOTARGS"))
static std::atomic<double> testSurveyTime{100};
#define FUXI_MTL_SURVEY_NOW() testSurveyTime.load()
static std::atomic<unsigned> backboardExits{0};
static void testSurveyDeadline(void (^callback)());
#define FUXI_MTL_SURVEY_TIMER(callback) testSurveyDeadline(callback)
#define FUXI_MTL_BACKBOARD_EXIT() (void)++backboardExits
#define _NSGetExecutablePath testExecutablePath
#define FUXI_MTL_FRONTEND_TEST
#define FUXI_MTL_BACKEND_HEADER "TestBackend.hpp"
#include "Frontend.mm"
static void (^surveyDeadline)();static unsigned deadlineArms;
static void testSurveyDeadline(void (^callback)()){assert(!surveyDeadline);surveyDeadline=[callback copy];++deadlineArms;}
extern "C" int testExecutablePath(char *path,uint32_t *size) {
 std::string s="/diagnostic/";s+=getenv("FUXI_TEST_PROCESS")?getenv("FUXI_TEST_PROCESS"):"gate-test";
 assert(*size>s.size());strcpy(path,s.c_str());return 0;
}
@interface FuxiGateBase : NSObject {uint8_t bytes[520];}
@end
@implementation FuxiGateBase
@end
@interface FuxiSurveyDrawable : NSObject { @public unsigned presents; }
@end
@implementation FuxiSurveyDrawable
- (void)present {++presents;}
- (void)presentAtTime:(CFTimeInterval)time {(void)time;++presents;}
- (void)presentAfterMinimumDuration:(CFTimeInterval)time {(void)time;++presents;}
@end
int main() {
 @autoreleasepool {
  Class cls=makeDeviceClass([FuxiGateBase class]);assert(cls);
  for(unsigned i=0;i<64;++i) {
   // An invalid port would fail after policy evaluation as well; no IOKit or
   // backend constructor is reachable through the denied branch.
   assert(![[cls alloc] initWithAcceleratorPort:(getenv("FUXI_TEST_GATE_ALLOW")?0:1)]);
  }
  unsigned expected=clientMode==fuxi::ClientMode::allow?(!strcmp(clientProcess,"backboardd")?2u:1u):0u;
  assert(preferenceWrites==expected+(expected && !strcmp(clientProcess,"backboardd")?1u:0u) && setupEnvironments==expected);
  assert(policyReads==1 && fuxi::Backend::constructed==0 && fuxi::Backend::live==0 && fuxi::Backend::submissions==0);
  if(getenv("FUXI_TEST_GATE_EXPECT"))assert((clientMode==fuxi::ClientMode::allow)==!strcmp(getenv("FUXI_TEST_GATE_EXPECT"),"allow"));
  if(getenv("FUXI_TEST_GATE_SURVEY")){
   assert(backboardSurvey.active() && !fuxi::backboardGate.blocked());
   // M9n: a legal aliased copy must reach completion before any refusal,
   // without poisoning the gate or arming the diagnostic deadline.
   {id device=[NSObject new];FuxiOwner *state=[FuxiOwner new];state->backend=new fuxi::Backend;
    objc_setAssociatedObject(device,&ownerKey,state,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[state release];fuxi::Backend::modelTextures=true;
    FuxiQueue *queue=[FuxiQueue new];queue->_device=[device retain];
    FuxiTexture *texture=[FuxiTexture new];texture->_device=[device retain];texture->_shared=YES;texture->_texture=owner(device)->backend->allocateTexture(80,5,8,4);
    uint8_t input[128];for(unsigned i=0;i<128;++i)input[i]=(uint8_t)i;
    [texture replaceRegion:MTLRegionMake2D(0,0,8,4) mipmapLevel:0 withBytes:input bytesPerRow:32];
    FuxiCommandBuffer *cb=(FuxiCommandBuffer *)[queue commandBuffer];id<MTLBlitCommandEncoder> blit=(id)[cb blitCommandEncoder];
    [blit copyFromTexture:(id)texture sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(7,3,1) toTexture:(id)texture destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(1,1,0)];
    [blit endEncoding];[cb commit];assert(!cb.error && cb.status==MTLCommandBufferStatusCompleted);
    assert(!fuxi::backboardGate.blocked() && !backboardSurvey.distinct() && !deadlineArms);
    [texture release];[queue release];[device release];}
   // A single refusal persists denial immediately, even beyond the normal log cap.
   failureCount=1000;failureLog("survey distinct first\n");assert(backboardExits==0 && fuxi::backboardGate.blocked());
   for(unsigned i=0;i<100;++i)failureLog("survey distinct first\n");assert(backboardSurvey.distinct()==1 && deadlineArms==1 && surveyDeadline);
   // Survey keeps resources and pipelines non-nil after the latch: CA aborts on a nil pipeline.
   assert(fuxi::backboardGate.surveying.load() && !fuxi::backboardGate.refusesResources());
   {NSError *err=[NSError errorWithDomain:@"t" code:1 userInfo:nil];id owner=[NSObject new];
    FuxiPipeline *placeholder=surveyPipeline(nil,&err,owner);assert(placeholder && !err && !placeholder->_pipeline);
    fuxi::backboardGate.surveying=false;assert(fuxi::backboardGate.refusesResources());fuxi::backboardGate.surveying=true;
    [placeholder release];[owner release];}
   id device=[NSObject new];FuxiOwner *state=[FuxiOwner new];state->backend=new fuxi::Backend;
   objc_setAssociatedObject(device,&ownerKey,state,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[state release];
   FuxiQueue *queue=[FuxiQueue new];queue->_device=[device retain];
   FuxiEvent *event=[FuxiEvent new];event->_device=[device retain];
   FuxiSurveyDrawable *drawable=[FuxiSurveyDrawable new];__block unsigned completed=0,scheduled=0;
   int submissions=fuxi::Backend::submissions;
   for(unsigned i=0;i<3;++i){FuxiCommandBuffer *cb=(FuxiCommandBuffer *)[queue commandBuffer];
    [cb addScheduledHandler:^(id<MTLCommandBuffer> b){assert(b.status==MTLCommandBufferStatusCompleted);++scheduled;}];
    [cb addCompletedHandler:^(id<MTLCommandBuffer> b){assert(b.status==MTLCommandBufferStatusCompleted && !b.error);++completed;}];
    // No-op waits never stall, and signal/listener/present bookkeeping completes.
    [cb encodeWaitForEvent:(id)event value:999];[cb encodeSignalEvent:(id)event value:i+1];
    if(!i)[cb presentDrawable:(id)drawable];else if(i==1)[cb presentDrawable:(id)drawable atTime:0];else [cb presentDrawable:(id)drawable afterMinimumDuration:0];
    cb->_rejected=YES;[cb commit];[cb waitUntilCompleted];assert(cb.status==MTLCommandBufferStatusCompleted);
   }
   assert(completed==3 && scheduled==3 && drawable->presents==3 && [event signaledValue]==3 && fuxi::Backend::submissions==submissions);
   if(!strcmp(getenv("FUXI_TEST_GATE_SURVEY"),"cap")){
    for(unsigned i=1;i<64;++i)failureLog("survey distinct %u\n",i);
    assert(backboardSurvey.distinct()==64 && backboardExits==1);
    failureLog("survey overflow\n");finishSurvey();assert(backboardExits==1);
   }else{
    testSurveyTime=119.999;finishSurvey();assert(backboardExits==0);testSurveyTime=120;surveyDeadline();assert(backboardExits==1);surveyDeadline();assert(backboardExits==1);
   }
   testSurveyTime=200;surveyDeadline();assert(backboardExits==1);[surveyDeadline release];surveyDeadline=nil;
   [drawable release];[event release];[queue release];[device release];
  }
  if(getenv("FUXI_TEST_GATE_FAULT")) {
   // Exercise actual commit bookkeeping: only a defined, stored IOSurface render
   // completion qualifies; empty/event-only/ordinary-texture work cannot mark good.
   fuxi::Backend::modelTextures=true;
   id device=[NSObject new];FuxiOwner *state=[FuxiOwner new];state->backend=new fuxi::Backend;
   objc_setAssociatedObject(device,&ownerKey,state,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[state release];
   FuxiQueue *queue=[FuxiQueue new];queue->_device=[device retain];
   FuxiTexture *texture=[FuxiTexture new];texture->_device=[device retain];texture->_texture=owner(device)->backend->allocateTexture();
   auto draw=[&](uint32_t store){FuxiCommandBuffer *cb=(FuxiCommandBuffer *)[queue commandBuffer];
    fuxi::Operation op{};op.texture=texture->_texture;op.clearOnly=true;op.store=store;op.clear[3]=1;
    cb->_ops.push_back(op);[cb->_resources addObject:texture];[cb commit];assert(cb.status==MTLCommandBufferStatusCompleted);};
   auto good=[&]{char data[128]={};std::string path=getenv("FUXI_TEST_GATE_DIR");path+="/gate.state";
    int fd=open(path.c_str(),O_RDONLY);assert(fd>=0);assert(read(fd,data,sizeof(data)-1)>0);close(fd);return strstr(data," good\n")!=nullptr;};
   for(unsigned i=0;i<120;++i){[[queue commandBuffer] commit];draw(1);}assert(!good());
   texture->_surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@256,(id)kIOSurfaceHeight:@256,
    (id)kIOSurfaceBytesPerElement:@4,(id)kIOSurfacePixelFormat:@0x42475241});
   assert(texture->_surface && !IOSurfaceLock(texture->_surface,0,nullptr));IOSurfaceIncrementUseCount(texture->_surface);
   for(unsigned i=0;i<120;++i)draw(0);assert(!good());
   for(unsigned i=0;i<fuxi::BackboardGate::goodFrames-1;++i)draw(1);assert(!good());draw(1);assert(good());
   [texture release];[queue release];[device release];
   // A refused IOSurface texture fails that texture only (h-daily2: a video plane's refusal sent backboardd to the CPU): the
   // armed gate stays good and nothing exits, while a plane the backend can import still imports.
   {fuxi::Backend::modelHostImport=true;id surfaceDevice=[cls alloc];FuxiOwner *surfaceState=[FuxiOwner new];surfaceState->backend=new fuxi::Backend;
    objc_setAssociatedObject(surfaceDevice,&ownerKey,surfaceState,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[surfaceState release];
    IOSurfaceRef video=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@332,(id)kIOSurfaceHeight:@198,(id)kIOSurfacePixelFormat:@0x34323076,(id)kIOSurfaceAllocSize:@131072,
     (id)kIOSurfacePlaneInfo:@[@{(id)kIOSurfacePlaneWidth:@332,(id)kIOSurfacePlaneHeight:@198,(id)kIOSurfacePlaneBytesPerElement:@1,(id)kIOSurfacePlaneBytesPerRow:@384,(id)kIOSurfacePlaneOffset:@64,(id)kIOSurfacePlaneSize:@79872},
      @{(id)kIOSurfacePlaneWidth:@166,(id)kIOSurfacePlaneHeight:@99,(id)kIOSurfacePlaneBytesPerElement:@2,(id)kIOSurfacePlaneBytesPerRow:@384,(id)kIOSurfacePlaneOffset:@79936,(id)kIOSurfacePlaneSize:@39936}]});
    assert(video);auto failures=failureCount.load();
    MTLTextureDescriptor *bgra=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:332 height:198 mipmapped:NO];bgra.usage=MTLTextureUsageShaderRead;bgra.storageMode=MTLStorageModeShared;
    assert(![surfaceDevice newTextureWithDescriptor:bgra iosurface:video plane:0] && ![surfaceDevice newTextureWithDescriptor:bgra iosurface:video plane:3]);
    MTLTextureDescriptor *r8=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR8Unorm width:332 height:198 mipmapped:NO];r8.usage=MTLTextureUsageShaderRead;r8.storageMode=MTLStorageModeShared;
    FuxiTexture *luma=(FuxiTexture *)[surfaceDevice newTextureWithDescriptor:r8 iosurface:video plane:0];
    assert(luma && luma.iosurfacePlane==0 && fuxi::Backend::importOffset==64);
    assert(!fuxi::backboardGate.blocked() && backboardExits==0 && failureCount==failures && good());
    [luma release];CFRelease(video);[surfaceDevice release];}
   // A texture FuxiMetal's own byte accounting refuses (BudgetRefusal: no Vulkan call failed) fails alone too. v-imp2c (5 Nov): CA's
   // 284x93 texture, refused by a budget full of Safari's surfaces, latched backboardd onto the CPU for the rest of the boot.
   {id budgetDevice=[cls alloc];FuxiOwner *budgetState=[FuxiOwner new];budgetState->backend=new fuxi::Backend;
    objc_setAssociatedObject(budgetDevice,&ownerKey,budgetState,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[budgetState release];
    budgetState->backend->limits.target=1u<<20; // the model's own target: 1 MiB
    auto failures=failureCount.load();auto soft=softRefusalCount.load();
    MTLTextureDescriptor *small=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:284 height:93 mipmapped:NO];
    small.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget;small.storageMode=MTLStorageModePrivate;
    MTLTextureDescriptor *large=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:1024 height:1024 mipmapped:NO];
    large.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget;large.storageMode=MTLStorageModePrivate;
    id first=[budgetDevice newTextureWithDescriptor:small];assert(first);
    assert(![budgetDevice newTextureWithDescriptor:large] && ![budgetDevice newTextureWithDescriptor:large]);
    assert(!fuxi::backboardGate.blocked() && backboardExits==0 && failureCount==failures && softRefusalCount==soft+2 && good());
    id second=[budgetDevice newTextureWithDescriptor:small];assert(second);
    [first release];[second release];[budgetDevice release];}
   // Even a failure after qualification and beyond the ordinary log cap latches.
   failureCount=64;failureLog("test failure after good\n");assert(fuxi::backboardGate.blocked());
   // A latched render server exits so launchd respawns it on the CPU path.
   assert(backboardExits==1);
  }
 }
 puts("PASS: gated init, 64 attempts one policy read/log, executable identity independent of argv");
}
