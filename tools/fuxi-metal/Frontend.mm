/* Bounded Metal diagnostic. No Apple code. CA resource/queue/encoder adapters adapted
 * from UniXNU NativeMetal, MIT, Copyright (c) 2026 NativeMetal contributors.
 * See LICENSE.NativeMetal and docs-lanes/metal-m9e.md for source provenance. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <IOSurface/IOSurfaceRef.h>
#import <objc/runtime.h>
#import <objc/message.h>
#include <unistd.h>
#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <mach/mach.h>
#include "NativeSelection.hpp"
#include "NativeBackend.hpp"
#include <mutex>
#include <atomic>
#include <cstdarg>
#include <chrono>
#include "ConstantHooks.hpp"
#include <cmath>
#include <cfloat>
#include <fcntl.h>
#include <sys/stat.h>
#include "ClientGate.hpp"
#include "ClientSetup.hpp"
#include "DrawSafety.hpp"
#include "SamplingPolicy.hpp"
#include "BackboardGate.hpp"
#include <thread>
#include <unordered_set>
#include <unordered_map>
#include <utility>
#include <memory>
#include "Survey.hpp"
#include "Translator.hpp"
#include "CAState.hpp"
#include "CommandStorage.hpp"
#include "Timeline.hpp"
#include "HostImportRange.hpp"
#include "SurfaceLease.hpp"
#include "VertexProfile.hpp"
#ifndef FUXI_MTL_BACKEND_HEADER
#define FUXI_MTL_BACKEND_HEADER "Backend.hpp"
#endif
#include FUXI_MTL_BACKEND_HEADER
extern "C" void _objc_msgForward(void);
#ifndef FUXI_MTL_CLIENTS_CONF
#define FUXI_MTL_CLIENTS_CONF "/usr/local/share/fuxi/metal-clients.conf"
#endif
static fuxi::ClientMode clientMode=fuxi::ClientMode::deny;
static char clientProcess[64]="unknown";
static bool gpuScanout=false,gpuScanoutQuiet=false;
// One worker avoids a compile stampede. Blocks own device, descriptor snapshot
// and handler until completion; the backend still serializes Vulkan/cache use.
static dispatch_queue_t pipelineCompilationQueue() {
 static dispatch_queue_t queue=dispatch_queue_create("org.fuxi.metal.pipelines",DISPATCH_QUEUE_SERIAL);return queue;
}

static int lockScanoutSurface(uintptr_t p,bool write) {return fuxi::frameKernelCall([&]{return IOSurfaceLock((IOSurfaceRef)p,write?0:kIOSurfaceLockReadOnly,nullptr);});}
static int lockScanoutPendingSurface(uintptr_t p,bool) {return fuxi::frameKernelCall([&]{return IOSurfaceLock((IOSurfaceRef)p,kIOSurfaceLockAvoidSync,nullptr);});}
static void unlockScanoutSurface(uintptr_t p,bool write) {fuxi::frameKernelCall([&]{(void)IOSurfaceUnlock((IOSurfaceRef)p,write?0:kIOSurfaceLockReadOnly,nullptr);});}
static fuxi::MetalBackend selectedBackend=fuxi::MetalBackend::fuxi;
#ifndef FUXI_MTL_BOOTARGS
#define FUXI_MTL_BOOTARGS(out,size) fuxi::systemString("kern.bootargs",out,size)
#endif
#ifndef FUXI_MTL_NATIVE_PREFLIGHT
#define FUXI_MTL_NATIVE_PREFLIGHT(port) nativeHostImportPreflight(port)
#endif
#ifndef FUXI_MTL_NATIVE_OPEN
#define FUXI_MTL_NATIVE_OPEN(process,registry,hooks) openNativeMetal(process,registry,hooks)
#endif
#ifndef FUXI_MTL_READ_CLIENTS
#define FUXI_MTL_READ_CLIENTS fuxi::readClients
#endif
#ifndef FUXI_MTL_BACKBOARD_EXIT
#define FUXI_MTL_BACKBOARD_EXIT() _exit(75)
#endif
#ifndef FUXI_MTL_BACKBOARD_ADMIT
#define FUXI_MTL_BACKBOARD_ADMIT() fuxi::backboardGate.admit()
#endif
#ifndef FUXI_MTL_BACKBOARD_MODE
#define FUXI_MTL_BACKBOARD_MODE() fuxi::systemBackboardMode()
#endif
// observeConstants is an __attribute__((constructor)) and reaches checkClient
// before this file's C++ initializers run; a file-scope reference was still
// null there and crashed backboardd (device, M9l survey boot). Construct on use.
static fuxi::Survey &surveyInstance(){static fuxi::Survey *survey=new fuxi::Survey;return *survey;}
#define backboardSurvey (surveyInstance())
#ifndef FUXI_MTL_SURVEY_NOW
#define FUXI_MTL_SURVEY_NOW() std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count()
#endif
static double surveyNow(){return FUXI_MTL_SURVEY_NOW();}
#ifndef FUXI_MTL_SURVEY_TIMER
#define FUXI_MTL_SURVEY_TIMER(callback) dispatch_after(dispatch_time(DISPATCH_TIME_NOW,20*NSEC_PER_SEC),dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_HIGH,0),callback)
#endif
static void finishSurvey(){if(backboardSurvey.finish(surveyNow())){fflush(stderr);FUXI_MTL_BACKBOARD_EXIT();}}
static uint64_t observationMicros() {return (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
static std::atomic<uint64_t> clientCheckUs{0};
#ifndef FUXI_MTL_SET_CA_PREFERENCE
static void setCACompressionPreference(const char *name){
 CFStringRef key=CFStringCreateWithCString(kCFAllocatorDefault,name,kCFStringEncodingUTF8);
 if(key){CFPreferencesSetAppValue(key,kCFBooleanFalse,kCFPreferencesCurrentApplication);CFRelease(key);}
}
#define FUXI_MTL_SET_CA_PREFERENCE(name) setCACompressionPreference(name)
#endif
#ifndef FUXI_MTL_SET_ENV
#define FUXI_MTL_SET_ENV(key,value,overwrite) setenv(key,value,overwrite)
#endif
static void checkClient() {
 static std::once_flag once;
 std::call_once(once,[]{
  // dyld's executable path, never argv[0] (exec-swap preserves daemon argv).
  bool identified=false;char path[4096];uint32_t size=sizeof(path);
  if(!_NSGetExecutablePath(path,&size)) {
   const char *name=strrchr(path,'/');name=name?name+1:path;
   if(fuxi::clientName(name)) {memcpy(clientProcess,name,strlen(name)+1);identified=true;}
  }
  clientMode=identified?FUXI_MTL_READ_CLIENTS(FUXI_MTL_CLIENTS_CONF,clientProcess):fuxi::ClientMode::deny;
  if(clientMode==fuxi::ClientMode::allow){
   char args[4096]={};
   (void)FUXI_MTL_BOOTARGS(args,sizeof(args));
   gpuScanout=fuxi::gpuScanoutEnabled(args);gpuScanoutQuiet=fuxi::translationOption(args,"sweetfb_noconsole=");
   if(gpuScanout)fprintf(stderr,"FUXI MTL: GPU scanout surface locks per submission\n");
   if(fuxi::earlyScheduledWait(fuxi::systemTranslationPolicy(),gpuScanout,gpuScanoutQuiet,clientProcess))fprintf(stderr,"FUXI MTL: CA scheduled wait early=1 commit-reserved surfaces\n");
   selectedBackend=fuxi::selectMetalBackend(args);if(selectedBackend==fuxi::MetalBackend::invalid)clientMode=fuxi::ClientMode::deny;
  }
  if(!strcmp(clientProcess,"backboardd")) {
   if(clientMode!=fuxi::ClientMode::allow)fprintf(stderr,"FUXI MTL: backboardd gate deny reason=policy-or-bootarg\n");
   else if(!FUXI_MTL_BACKBOARD_ADMIT())clientMode=fuxi::ClientMode::deny;
   else {
    backboardSurvey.enable(FUXI_MTL_BACKBOARD_MODE()==2);
    fuxi::backboardGate.surveying=backboardSurvey.active();
    if(backboardSurvey.active())fprintf(stderr,"FUXI MTL: backboardd survey enabled diagnostic: screen may blank for up to 20s after refusal\n");
    // Lifetime diagnostics include idle periods and a stalled render thread.
    static dispatch_source_t timer=dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER,0,0,dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_LOW,0));
    if(timer){dispatch_source_set_timer(timer,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC),5*NSEC_PER_SEC,NSEC_PER_SEC/10);
     dispatch_source_set_event_handler(timer,^{fuxi::backboardGate.report();finishSurvey();});dispatch_resume(timer);}
   }
  }
  // Constructor-time admission precedes CA framebuffer selection and discovery.
  // Census/denied clients must not change the process's rendering preferences.
  fuxi::setupMetalClient(clientMode==fuxi::ClientMode::allow,clientProcess,
   [](const char *key,const char *value,int overwrite){return FUXI_MTL_SET_ENV(key,value,overwrite);},
   [](const char *key){FUXI_MTL_SET_CA_PREFERENCE(key);});
  if(clientMode==fuxi::ClientMode::allow)fprintf(stderr,"FUXI MTL: backend %s\n",selectedBackend==fuxi::MetalBackend::native?"native":"fuxi");
  if(clientMode==fuxi::ClientMode::allow)fprintf(stderr,"FUXI MTL: compression %s CAEnableIOSurfaceCompression=false%s\n",clientProcess,
   !strcmp(clientProcess,"backboardd")?" CA_DISABLE_FRAMEBUFFER_COMPRESSION=1 CAEnableFramebufferCompression=false CAEnableDeepFramebuffer=false":"");
  clientCheckUs=observationMicros();
  fprintf(stderr,"FUXI MTL: gate %s %s\n",clientMode==fuxi::ClientMode::deny?"deny":"allow",clientProcess);
  if(clientMode==fuxi::ClientMode::census)fprintf(stderr,"FUXI MTL: census enabled %s (no GPU)\n",clientProcess);
 });
}
// One bounded table per process, shared by every frontend object. No retained
// objects/NSStrings and no allocation once full. Repeated messages stay silent.
struct CensusEntry {Class cls;SEL selector;};
static CensusEntry censusEntries[256];static size_t censusCount;static std::mutex censusLock;
static bool caTrace() {return !strcmp(clientProcess,"fuxi-ca-smoke");}
static void censusLog(id object,SEL selector,bool implemented) {
 // The isolated CA smoke also traces each class/selector's first use (diagnostic only).
 if(clientMode!=fuxi::ClientMode::census && !caTrace())return;
 Class cls=object_getClass(object);std::lock_guard<std::mutex> lock(censusLock);
 for(size_t i=0;i<censusCount;++i)if(censusEntries[i].cls==cls && censusEntries[i].selector==selector)return;
 if(censusCount==256)return;
 censusEntries[censusCount++]={cls,selector};
 fprintf(stderr,"FUXI MTL: %s %s %s %s %s\n",clientMode==fuxi::ClientMode::census?"census":"trace",clientProcess,class_getName(cls),sel_getName(selector),implemented?"implemented":"unsupported");
 if(censusCount==256)fprintf(stderr,"FUXI MTL: census limit %s 256\n",clientProcess);
}
#define CENSUS() censusLog(self,_cmd,true)
#define FUXI_PHASE(x) fuxi::PhaseTimer fuxiPhase(fuxi::EncPhase::x)
// Diagnostic-only process counters. A client can refuse an entire candidate
// after CA swallowed a nil/error; these counters never grant client admission.
static std::atomic<uint64_t> failureCount{0},completedDraws{0};
extern "C" uint64_t fuxi_metal_failure_count() {return failureCount.load();}
extern "C" uint64_t fuxi_metal_completed_draws() {return completedDraws.load();}
extern "C" uint64_t fuxi_metal_dropped_draws() {return fuxi::droppedDraws.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_hits() {return selectedBackend==fuxi::MetalBackend::native?nativeMetric(0):fuxi::Backend::pipelineHits.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_misses() {return selectedBackend==fuxi::MetalBackend::native?nativeMetric(1):fuxi::Backend::pipelineMisses.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_load_result() {return fuxi::Backend::pipelineCacheLoadResult.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_load_bytes() {return fuxi::Backend::pipelineCacheLoadBytes.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_save_result() {return fuxi::Backend::pipelineCacheSaveResult.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_save_bytes() {return fuxi::Backend::pipelineCacheSaveBytes.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_save_count() {return fuxi::Backend::pipelineCacheSaveCount.load();}
extern "C" uint64_t fuxi_metal_pipeline_cache_save_failures() {return fuxi::Backend::pipelineCacheSaveFailures.load();}
extern "C" uint64_t fuxi_metal_gpu_nanoseconds() {return fuxi::Backend::gpuNanos.load();}
extern "C" uint64_t fuxi_metal_gpu_samples() {return fuxi::Backend::gpuSamples.load();}
extern "C" uint64_t fuxi_metal_submits() {return fuxi::Backend::submits.load();}
extern "C" uint64_t fuxi_metal_submit_prepare_nanoseconds() {return fuxi::Backend::submitPrepareNanos.load();}
extern "C" uint64_t fuxi_metal_submit_wait_nanoseconds() {return fuxi::Backend::submitWaitNanos.load();}
extern "C" uint64_t fuxi_metal_submit_retire_nanoseconds() {return fuxi::Backend::submitRetireNanos.load();}
extern "C" uint64_t fuxi_metal_submit_objects_nanoseconds() {return fuxi::Backend::submitObjectsNanos.load();}
extern "C" uint64_t fuxi_metal_submit_descriptors_nanoseconds() {return fuxi::Backend::submitDescriptorsNanos.load();}
extern "C" uint64_t fuxi_metal_submit_record_nanoseconds() {return fuxi::Backend::submitRecordNanos.load();}
extern "C" uint64_t fuxi_metal_submit_shadow_nanoseconds() {return fuxi::Backend::shadowCopyNanos.load();}
extern "C" uint64_t fuxi_metal_submit_flush_nanoseconds() {return fuxi::Backend::submitFlushNanos.load();}
extern "C" uint64_t fuxi_metal_submit_invalidate_nanoseconds() {return fuxi::Backend::submitInvalidateNanos.load();}
extern "C" uint64_t fuxi_metal_submit_flush_calls() {return fuxi::Backend::flushCalls.load();}
extern "C" uint64_t fuxi_metal_submit_invalidate_calls() {return fuxi::Backend::invalidateCalls.load();}
extern "C" uint64_t fuxi_metal_submit_empty() {return fuxi::Backend::emptySubmits.load();}
extern "C" uint64_t fuxi_metal_submit_object_creates() {return fuxi::Backend::submitObjectCreates.load();}
extern "C" uint64_t fuxi_metal_submit_pool_creates() {return fuxi::Backend::submitPoolCreates.load();}
extern "C" uint64_t fuxi_metal_submit_frame_creates() {return fuxi::Backend::submitFrameCreates.load();}
extern "C" uint64_t fuxi_metal_sampling_skipped_draws() {return fuxi::skippedSamplingDraws.load();}
extern "C" uint64_t fuxi_metal_submit_view_creates() {return fuxi::Backend::submitViewCreates.load();}
struct DescriptorText {NSString *text=nil;const fuxi::LazyText *lazy=nullptr;};
static thread_local DescriptorText descriptorContext;
struct DescriptorContext {
 DescriptorText previous;
 explicit DescriptorContext(NSString *text):previous(descriptorContext){descriptorContext={text,nullptr};}
 explicit DescriptorContext(const fuxi::LazyText &text):previous(descriptorContext){descriptorContext={nil,&text};}
 ~DescriptorContext(){descriptorContext=previous;}
};
static qos_class_t compositorQoS(){return strcmp(clientProcess,"backboardd")?QOS_CLASS_DEFAULT:QOS_CLASS_USER_INTERACTIVE;}
static void failureLog(const char *format,...) {
 fuxi::backboardGate.fault();uint64_t n=failureCount.fetch_add(1);
 char message[fuxi::Survey::messageBytes];va_list args;va_start(args,format);vsnprintf(message,sizeof(message),format,args);va_end(args);
 // Context is captured in the refusal itself, before the normal immediate exit.
 if(descriptorContext.text || descriptorContext.lazy){size_t len=strlen(message);if(len && message[len-1]=='\n')message[--len]=0;
  snprintf(message+len,sizeof(message)-len," desc=%s\n",descriptorContext.text?descriptorContext.text.UTF8String:descriptorContext.lazy->c_str());}
 // Keep each refusal a single complete console line, including translator errors.
 size_t length=strlen(message);for(size_t i=0;i+1<length;++i)if(message[i]=='\n' || message[i]=='\r')message[i]=' ';
 if(!length || message[length-1]!='\n'){if(length>=sizeof(message)-1)length=sizeof(message)-2;message[length++]='\n';message[length]=0;}
 if(backboardSurvey.active() && fuxi::backboardGate.blocked()){
  (void)backboardSurvey.record(message,surveyNow(),true);
  static std::once_flag deadline;std::call_once(deadline,[]{
   FUXI_MTL_SURVEY_TIMER(^{finishSurvey();});
  });
  finishSurvey();return;
 }
 if(n<64){fputs(message,stderr);if(n==63)fprintf(stderr,"FUXI MTL: failure log limit 64 (counter continues)\n");}
 if(fuxi::backboardGate.blocked()){
  fprintf(stderr,"FUXI MTL: backboardd gate exit for CPU respawn\n");fflush(stderr);FUXI_MTL_BACKBOARD_EXIT();
 }
}
static void unsupported(id object, SEL selector) {
 if(clientMode==fuxi::ClientMode::census) {censusLog(object,selector,false);return;}
 failureLog("FUXI MTL: unsupported %.96s %.160s\n",class_getName(object_getClass(object)),sel_getName(selector));
}
static void invalid(NSString *why) {
 failureLog("FUXI MTL: invalid %s\n",why.UTF8String);
 [NSException raise:NSInvalidArgumentException format:@"Fuxi Metal: %@",why];
}
@interface FuxiObject : NSObject
- (void)rejectSelector:(SEL)s;
@end
@implementation FuxiObject
- (void)rejectSelector:(SEL)s {unsupported(self,s);}
- (NSMethodSignature *)methodSignatureForSelector:(SEL)s {
 Method method=class_getInstanceMethod(object_getClass(self),s);
 if(method)return [NSMethodSignature signatureWithObjCTypes:method_getTypeEncoding(method)];
 {
  unsigned count=0;Protocol *__unsafe_unretained *protocols=objc_copyProtocolList(&count);
  const char *types=nullptr;
  for(unsigned i=0;i<count && !types;++i)for(int required=0;required<2 && !types;++required)
   types=protocol_getMethodDescription(protocols[i],s,required,YES).types;
  free(protocols);
  if(types)return [NSMethodSignature signatureWithObjCTypes:types];
 }
 // A private selector absent from every loaded protocol/class has no type ABI.
 // Use the conventional object/word failure signature, without reading args.
 return [NSMethodSignature signatureWithObjCTypes:"@@:"];
}
- (void)doesNotRecognizeSelector:(SEL)s {[self rejectSelector:s];}
- (void)forwardInvocation:(NSInvocation *)inv {
 [self rejectSelector:inv.selector];
 // Public protocols / inherited methods supply the exact return ABI, including
 // structs. Missing encoder methods poison their command; commit never submits.
 NSUInteger n=inv.methodSignature.methodReturnLength;
 if(n) {void *zero=calloc(1,n);[inv setReturnValue:zero];free(zero);}
}

@end
// Resource/command metadata only. Keep ivars out of FuxiObject: its methods
// are also copied onto the runtime device subclass, whose layout is private.
@interface FuxiLabeledObject : FuxiObject { @public NSString *_label; }
- (NSString *)label;
- (void)setLabel:(NSString *)s;
@end
@implementation FuxiLabeledObject
- (void)dealloc {[_label release];[super dealloc];}
- (void)insertDebugSignpost:(NSString *)s {CENSUS();(void)s;}
- (void)pushDebugGroup:(NSString *)s {CENSUS();(void)s;}
- (void)popDebugGroup {CENSUS();}
- (NSString *)label {CENSUS();return _label;}
- (void)setLabel:(NSString *)s {CENSUS();NSString *next=[s copy];[_label release];_label=next;}
@end
@interface FuxiOwner : NSObject { @public fuxi::Backend *backend; uint64_t registry; }
@end
@implementation FuxiOwner
- (void)dealloc {delete backend;[super dealloc];}
@end
static char ownerKey;
static FuxiOwner *owner(id obj) {return (FuxiOwner *)objc_getAssociatedObject(obj,&ownerKey);}
// NativeMetal resource SPI: allocations remain resident; purgeability never
// discards live storage, and owner identity is explicitly not applied.
@protocol FuxiHeapBudget <NSObject>
- (void)fuxiRelinquishBytes:(NSUInteger)bytes;
@end
@interface FuxiResource : FuxiLabeledObject { @public id _device;id<FuxiHeapBudget> _parentHeap;NSUInteger _heapBytes;MTLPurgeableState _purgeable; }
@end
@implementation FuxiResource
- (void)dealloc {[_parentHeap fuxiRelinquishBytes:_heapBytes];[_parentHeap release];[super dealloc];}
- (id)device {CENSUS();return _device;}
- (NSUInteger)protectionOptions {CENSUS();return 0;}
- (id)heap {CENSUS();return _parentHeap;}
- (BOOL)isAliasable {CENSUS();return NO;}
- (int)setOwnerWithIdentity:(unsigned)identity {CENSUS();(void)identity;return 46;}
- (void)waitUntilComplete {CENSUS();} // Backend submissions complete on the caller.
- (MTLPurgeableState)setPurgeableState:(MTLPurgeableState)state {CENSUS();
 if(state<MTLPurgeableStateKeepCurrent || state>MTLPurgeableStateEmpty){unsupported(self,_cmd);return (MTLPurgeableState)0;}
 if(_parentHeap || [(id)self allocatedSize]<16384)return MTLPurgeableStateNonVolatile;
 @synchronized(self){MTLPurgeableState before=_purgeable?(MTLPurgeableState)_purgeable:MTLPurgeableStateNonVolatile;
  if(state!=MTLPurgeableStateKeepCurrent)_purgeable=state;return before;}
}
@end
@interface FuxiBuffer : FuxiResource { @public fuxi::Buffer *_buffer;void *_wrapped;NSUInteger _wrappedLength;void (^_deallocator)(void *,NSUInteger);BOOL _shadow;MTLStorageMode _storage;MTLCPUCacheMode _cacheMode; }
@end
@implementation FuxiBuffer
- (void)dealloc {auto *backend=owner(_device)->backend;backend->destroy(_buffer);
 if(!backend->poisoned){if(_deallocator)_deallocator(_wrapped,_wrappedLength);[_deallocator release];}
 // On uncertain ownership retain the no-copy deallocator and its pages.
 [_device release];[super dealloc];}
- (id)device {CENSUS();return _device;}
- (void *)contents {CENSUS();return _storage==MTLStorageModePrivate?nullptr:_shadow?_wrapped:_buffer->contents;}
- (NSUInteger)length {CENSUS();return _buffer->length;}
- (MTLPurgeableState)setPurgeableState:(MTLPurgeableState)state {CENSUS();
 MTLPurgeableState before=[super setPurgeableState:state];
 if(state==MTLPurgeableStateEmpty && !_parentHeap && _buffer->allocated>=16384){
  if(owner(_device)->backend->poisoned)invalid(@"purge after uncertain GPU ownership");memset(_buffer->contents,0,_buffer->length);
 }return before;
}
- (void)didModifyRange:(NSRange)r {CENSUS();if(r.location>[self length] || r.length>[self length]-r.location)invalid(@"buffer modified range");}
- (IOSurfaceRef)iosurface {CENSUS();return nullptr;}
- (void)getBytes:(void *)bytes range:(NSRange)r {CENSUS();
 if(_storage==MTLStorageModePrivate || !bytes || r.location>[self length] || r.length>[self length]-r.location)invalid(@"buffer getBytes range");
 memcpy(bytes,(uint8_t *)_buffer->contents+r.location,r.length);
}
- (MTLStorageMode)storageMode {CENSUS();return _storage;}
- (MTLCPUCacheMode)cpuCacheMode {CENSUS();return _cacheMode;}
- (MTLHazardTrackingMode)hazardTrackingMode {CENSUS();return MTLHazardTrackingModeTracked;}
- (MTLResourceOptions)resourceOptions {CENSUS();return (MTLResourceOptions)((_storage<<MTLResourceStorageModeShift)|(_cacheMode<<MTLResourceCPUCacheModeShift));}
- (NSUInteger)allocatedSize {CENSUS();return _buffer->allocated;}
@end
static id metalError(NSError **error,const char *why) {
 failureLog("FUXI MTL: pipeline/library FAIL %.768s\n",why);
 if(error)*error=[NSError errorWithDomain:@"org.fuxi.metal" code:1 userInfo:@{NSLocalizedDescriptionKey:[NSString stringWithUTF8String:why]}];return nil;
}
// Track only public calls. Allocation provenance and method-coverage epochs
// prevent pre-hook objects or missed overrides from acquiring a trusted snapshot.
static char constantsKey,constantsAllocationKey;
static std::atomic<uint64_t> constantsConstructorUs{0},constantsInstallUs{0};
static fuxi::ClientMode constantsConstructorMode=fuxi::ClientMode::deny;
static IMP makeConstantHook(fuxi::ConstantMethod,IMP,SEL);
#ifndef FUXI_MTL_CONSTANT_CLASS
#define FUXI_MTL_CONSTANT_CLASS MTLFunctionConstantValues
#endif
static fuxi::ConstantHooks &constantHooks() {
 // Function-local state is safe even when the dyld constructor precedes C++
 // dynamic initializers. Each method gets its own captured original IMP, so
 // subclass overrides calling super cannot recurse through a global original.
 // The installed IMPs can run during process teardown; keep their registry alive.
 static auto *hooks=new fuxi::ConstantHooks([FUXI_MTL_CONSTANT_CLASS class],makeConstantHook);return *hooks;
}
static size_t constantSize(MTLDataType t) {return fuxi::constantPayloadSize((uint32_t)t);}
static bool constantsObserved(id self) {
 uint64_t stamp=constantHooks().stamp(object_getClass(self));
 id allocation=objc_getAssociatedObject(self,&constantsAllocationKey);
 return stamp && [allocation isKindOfClass:[NSNumber class]] && [allocation unsignedLongLongValue]==stamp;
}
static void rememberConstant(id self,const void *value,MTLDataType type,id key) {
 id old=objc_getAssociatedObject(self,&constantsKey);size_t size=constantSize(type);
 if(!constantsObserved(self) || ![old isKindOfClass:[NSDictionary class]] || !value || !size || !key || ([old count]>=fuxi::maxFunctionConstants && !old[key])) {failureLog("FUXI MTL: constant reject type=%u bytes=%zu records=%lu\n",(unsigned)type,size,[old isKindOfClass:[NSDictionary class]]?(unsigned long)[old count]:0UL);objc_setAssociatedObject(self,&constantsKey,@NO,OBJC_ASSOCIATION_RETAIN_NONATOMIC);return;}
 NSMutableDictionary *next=[old mutableCopy];next[key]=@[@(type),[NSData dataWithBytes:value length:size]];
 objc_setAssociatedObject(self,&constantsKey,next,OBJC_ASSOCIATION_COPY_NONATOMIC);[next release];
}
static id allocatedConstants(id result) {
 if(result) {constantHooks().install(object_getClass(result));@synchronized(result){
  if(!objc_getAssociatedObject(result,&constantsAllocationKey)) {
   uint64_t stamp=constantHooks().stamp(object_getClass(result));
   if(stamp) {objc_setAssociatedObject(result,&constantsAllocationKey,@(stamp),OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    objc_setAssociatedObject(result,&constantsKey,@{},OBJC_ASSOCIATION_COPY_NONATOMIC);}
  }
 }}return result;
}
static IMP makeConstantHook(fuxi::ConstantMethod kind,IMP original,SEL sel) {
 switch(kind) {
 case fuxi::ConstantInit:return imp_implementationWithBlock(^id(id self){return ((id(*)(id,SEL))original)(self,sel);});
 case fuxi::ConstantCopy:return imp_implementationWithBlock(^id(id self,NSZone *zone){@synchronized(self){
  bool observed=constantsObserved(self);id records=[objc_getAssociatedObject(self,&constantsKey) retain];
  id result=((id(*)(id,SEL,NSZone *))original)(self,sel,zone);
  if(result && result!=self) {constantHooks().install(object_getClass(result));
   uint64_t stamp=constantHooks().stamp(object_getClass(result));
   objc_setAssociatedObject(result,&constantsAllocationKey,observed && stamp?@(stamp):nil,OBJC_ASSOCIATION_RETAIN_NONATOMIC);
   objc_setAssociatedObject(result,&constantsKey,observed?records:nil,OBJC_ASSOCIATION_COPY_NONATOMIC);}
  [records release];return result;
 }});
 case fuxi::ConstantIndex:return imp_implementationWithBlock(^(id self,const void *value,MTLDataType type,NSUInteger index){@synchronized(self){
  ((void(*)(id,SEL,const void *,MTLDataType,NSUInteger))original)(self,sel,value,type,index);
  rememberConstant(self,value,type,index<=65535?@(index):nil);
 }});
 case fuxi::ConstantName:return imp_implementationWithBlock(^(id self,const void *value,MTLDataType type,NSString *name){@synchronized(self){
  ((void(*)(id,SEL,const void *,MTLDataType,NSString *))original)(self,sel,value,type,name);
  const char *utf8=name.UTF8String;size_t n=[name lengthOfBytesUsingEncoding:NSUTF8StringEncoding];
  rememberConstant(self,value,type,utf8 && n && n<128 && strlen(utf8)==n?name:nil);
 }});
 case fuxi::ConstantRange:return imp_implementationWithBlock(^(id self,const void *value,MTLDataType type,NSRange range){@synchronized(self){
  ((void(*)(id,SEL,const void *,MTLDataType,NSRange))original)(self,sel,value,type,range);size_t size=constantSize(type);
  if(!constantsObserved(self) || !size || range.length>fuxi::maxFunctionConstants || range.location>65535 || range.length>65536-range.location || !value){failureLog("FUXI MTL: constant range reject type=%u location=%lu length=%lu\n",(unsigned)type,(unsigned long)range.location,(unsigned long)range.length);objc_setAssociatedObject(self,&constantsKey,@NO,OBJC_ASSOCIATION_RETAIN_NONATOMIC);return;}
  for(NSUInteger i=0;i<range.length;++i)rememberConstant(self,(const uint8_t *)value+i*fuxi::constantElementStride((uint32_t)type),type,@(range.location+i));
 }});
 case fuxi::ConstantReset:return imp_implementationWithBlock(^(id self){@synchronized(self){
  ((void(*)(id,SEL))original)(self,sel);
  objc_setAssociatedObject(self,&constantsKey,constantsObserved(self)?@{}:nil,OBJC_ASSOCIATION_COPY_NONATOMIC);
 }});
 case fuxi::ConstantAlloc:return imp_implementationWithBlock(^id(id self,NSZone *zone){
  // Inherited factories cover late-registered subclasses before init/setters.
  constantHooks().install((Class)self);
  return allocatedConstants(((id(*)(id,SEL,NSZone *))original)(self,sel,zone));
 });
 case fuxi::ConstantNew:return imp_implementationWithBlock(^id(id self){
  constantHooks().install((Class)self);return ((id(*)(id,SEL))original)(self,sel);
 });
 case fuxi::ConstantPlainAlloc:return imp_implementationWithBlock(^id(id self){
  constantHooks().install((Class)self);return allocatedConstants(((id(*)(id,SEL))original)(self,sel));
 });
 case fuxi::ConstantMethodCount:break;
 }return nullptr;
}
static void refreshConstantHooks() {
#ifndef FUXI_MTL_FRONTEND_TEST
 if(clientMode!=fuxi::ClientMode::allow)return;
#endif
 constantHooks().refresh();uint64_t unset=0;
 constantsInstallUs.compare_exchange_strong(unset,observationMicros());
}
__attribute__((constructor)) static void observeConstants() {
 constantsConstructorUs=observationMicros();
#ifndef FUXI_MTL_FRONTEND_TEST
 checkClient();
#endif
 constantsConstructorMode=clientMode;refreshConstantHooks();
 fprintf(stderr,"FUXI MTL: constants observe mode=%s ctor_us=%llu gate_us=%llu install_us=%llu hooks=%zu\n",
  clientMode==fuxi::ClientMode::allow?"allow":clientMode==fuxi::ClientMode::census?"census":"deny",
  (unsigned long long)constantsConstructorUs.load(),(unsigned long long)clientCheckUs.load(),(unsigned long long)constantsInstallUs.load(),constantsInstallUs.load()?constantHooks().count():0);
}
static NSData *constantFailure(id values,const char *reason) {
 if(!strcmp(clientProcess,"fuxi-ca-smoke")) {
  failureLog("FUXI MTL: constants snapshot FAIL reason=%.96s class=%.96s\n",reason,values?object_getClassName(values):"nil");return nil;
 }
 failureCount.fetch_add(1);
 // Eight lines/process maximum; no private layout, constant bytes or paths.
 static std::atomic<unsigned> failures{0};if(failures.fetch_add(1)>=8)return nil;
 fuxi::ConstantDiagnostic diagnostic;
 Class cls=object_getClass(values);id record=objc_getAssociatedObject(values,&constantsKey);
 diagnostic.append("FUXI MTL: constants snapshot FAIL reason=%s class=%.96s chain=",reason,cls?class_getName(cls):"nil");
 Class c=cls;unsigned depth=0;for(;c && depth<8;c=class_getSuperclass(c),++depth)diagnostic.append("%s%.80s",depth?">":"",class_getName(c));if(c)diagnostic.append(">...");
 diagnostic.append(" ctor_us=%llu gate_us=%llu install_us=%llu ctor_mode=%u mode=%u hooks=%zu record=%.80s/count=%ld allocation=%llu coverage=%llu",
  (unsigned long long)constantsConstructorUs.load(),(unsigned long long)clientCheckUs.load(),(unsigned long long)constantsInstallUs.load(),
  (unsigned)constantsConstructorMode,(unsigned)clientMode,constantHooks().count(),record?object_getClassName(record):"nil",
  [record isKindOfClass:[NSDictionary class]]?(long)[record count]:-1L,
  (unsigned long long)[objc_getAssociatedObject(values,&constantsAllocationKey) unsignedLongLongValue],(unsigned long long)constantHooks().stamp(cls));
 for(unsigned i=0;i<fuxi::ConstantMethodCount;++i) {
  auto h=constantHooks().resolved(cls,(fuxi::ConstantMethod)i);Class target=i>=fuxi::ConstantAlloc?object_getClass((id)cls):cls;
  IMP actual=class_getMethodImplementation(target,sel_registerName(fuxi::constantSelectors[i]));
  diagnostic.append(" %s=%p/%p/%p",fuxi::constantSelectors[i],(void *)actual,(void *)h.hook,(void *)h.original);
 }
 diagnostic.append(" imp=actual/hook/original");fprintf(stderr,"%s\n",diagnostic.line);return nil;
}
static NSData *constantSnapshot(MTLFunctionConstantValues *values) {
 if(!values)return [NSData data];refreshConstantHooks();@synchronized(values){
 if(!objc_getAssociatedObject(values,&constantsAllocationKey))return constantFailure(values,"unobserved-allocation");
 if(!constantsObserved(values))return constantFailure(values,"changed-or-unhooked-method");
 id records=objc_getAssociatedObject(values,&constantsKey);
 if(![records isKindOfClass:[NSDictionary class]] || [records count]>fuxi::maxFunctionConstants)return constantFailure(values,"unsupported-record");
 NSMutableData *data=[NSMutableData data];
 // Deterministic order makes equal constants share the translator cache.
 NSArray *keys=[[records allKeys] sortedArrayUsingComparator:^NSComparisonResult(id a,id b){return [[a description] compare:[b description]];}];
 for(id key in keys){NSArray *record=records[key];fuxi::Constant c{};c.type=[record[0] unsignedIntValue];NSData *bytes=record[1];c.size=(uint32_t)bytes.length;memcpy(c.bytes,bytes.bytes,bytes.length);
 if([key isKindOfClass:[NSNumber class]])c.index=[key unsignedIntValue];else {const char *name=[key UTF8String];if(!name || strlen(name)>=128)return constantFailure(values,"unsupported-name");strcpy(c.name,name);}
 [data appendBytes:&c length:sizeof(c)];}return data;}
}
@interface FuxiLibrary : FuxiLabeledObject { @public id _device; void *_library; }
- (id)fuxiNewFunctionWithName:(NSString *)name error:(NSError **)error;
@end
@interface FuxiFunction : FuxiLabeledObject { @public FuxiLibrary *_library; NSString *_name; NSData *_constants; int _stage; }
@end
@implementation FuxiFunction
- (void)dealloc {[_library release];[_name release];[_constants release];[super dealloc];}
- (id)device {CENSUS();return _library->_device;}
- (NSString *)name {CENSUS();return _name;}
- (MTLFunctionType)functionType {CENSUS();return _stage==1?MTLFunctionTypeVertex:MTLFunctionTypeFragment;}
- (MTLFunctionOptions)options {CENSUS();return MTLFunctionOptionNone;}
- (MTLFunctionDescriptor *)functionDescriptor {CENSUS();
 MTLFunctionDescriptor *d=[MTLFunctionDescriptor functionDescriptor];d.name=_name;
 if(_constants.length){MTLFunctionConstantValues *values=[MTLFunctionConstantValues new];
  const auto *constants=(const fuxi::Constant *)_constants.bytes;
  for(NSUInteger i=0;i<_constants.length/sizeof(fuxi::Constant);++i){const auto &c=constants[i];
   if(c.name[0])[values setConstantValue:c.bytes type:(MTLDataType)c.type withName:[NSString stringWithUTF8String:c.name]];
   else [values setConstantValue:c.bytes type:(MTLDataType)c.type atIndex:c.index];
  }d.constantValues=values;[values release];
 }return d;
}
@end
@implementation FuxiLibrary
- (MTLLibraryType)type {CENSUS();return MTLLibraryTypeExecutable;}
- (void)dealloc {if(_library){if(_device)owner(_device)->backend->unregisterLibrary(_library);fuxi::Translator::get().close(_library);}[_device release];[super dealloc];}
- (id)device {CENSUS();return _device;}
- (id)newFunctionWithDescriptor:(MTLFunctionDescriptor *)d error:(NSError **)error {CENSUS();
 if(error)*error=nil;
 // Constants are immutable snapshots of observed public setter calls.
 if(!d || d.specializedName || d.options!=MTLFunctionOptionNone) {
  unsupported(self,_cmd);return metalError(error,"unsupported function specialization/options");
 }
 return [self newFunctionWithName:d.name constantValues:d.constantValues error:error];
}
- (id)newFunctionWithName:(NSString *)name constantValues:(MTLFunctionConstantValues *)values error:(NSError **)error {CENSUS();
 if(error)*error=nil;NSData *snapshot=constantSnapshot(values);
 if(!snapshot)return metalError(error,"unobserved/unsupported function constants");
 FuxiFunction *f=[self fuxiNewFunctionWithName:name error:error];if(!f)return nil;
 f->_constants=[snapshot copy];return f;
}
- (void)newFunctionWithName:(NSString *)name constantValues:(MTLFunctionConstantValues *)values completionHandler:(void (^)(id<MTLFunction>,NSError *))handler {CENSUS();
 if(!handler){unsupported(self,_cmd);return;}NSError *error=nil;id function=[self newFunctionWithName:name constantValues:values error:&error];handler(function,error);[function release];
}
- (id)newFunctionWithName:(NSString *)name {CENSUS();
 return [self fuxiNewFunctionWithName:name error:nullptr];
}
- (id)fuxiNewFunctionWithName:(NSString *)name error:(NSError **)error {
 if(error)*error=nil;
 if(!name || !name.length || name.length>127 || !name.UTF8String || strlen(name.UTF8String)!=[name lengthOfBytesUsingEncoding:NSUTF8StringEncoding])return metalError(error,"invalid function name");
 // A lookup refusal returns nil/NSError, never a placeholder or a renamed
 // shader. CA itself retries then aborts on nil; see metal-m9c.md.
 char why[2048]{};int stage=0;
#ifndef FUXI_MTL_FUNCTION_LOOKUP
#define FUXI_MTL_FUNCTION_LOOKUP fuxi::Translator::get().function
#endif
 try {stage=FUXI_MTL_FUNCTION_LOOKUP(_library,name.UTF8String,why,sizeof(why));}
 catch(const std::exception &e) {return metalError(error,e.what());}
 if(!stage)return metalError(error,why[0]?why:"function lookup failed");
 FuxiFunction *f=[FuxiFunction new];f->_library=[self retain];f->_name=[name copy];f->_stage=stage;
 fprintf(stderr,"FUXI MTL: function %s class=%s stage=%d device=%u\n",name.UTF8String,object_getClassName(f),stage,[f device]==_device);return f;
}
@end
// Log the public value that failed, without inspecting Apple's object layout.
static bool stage4Value(const char *scope,const char *field,uint64_t value,bool accepted) {
 if(!accepted)failureLog("FUXI MTL: %s reject %s %llu\n",scope,field,(unsigned long long)value);
 return accepted;
}
static bool stage4Class(const char *scope,const char *field,id object,Class expected) {
 if([object isKindOfClass:expected])return true;
 failureLog("FUXI MTL: %s reject %s %s\n",scope,field,object?object_getClassName(object):"nil");return false;
}
static void stage4Require(const char *scope,const char *field,uint64_t value,bool accepted) {
 if(!stage4Value(scope,field,value,accepted))invalid([NSString stringWithFormat:@"%s %s",scope,field]);
}
static NSString *vertexAttributeText(MTLVertexDescriptor *vd,unsigned index) {
 auto a=vd.attributes[index];
 // Invalid buffer indices must never become an indexed SDK access.
 MTLVertexBufferLayoutDescriptor *l=a.bufferIndex<31?vd.layouts[a.bufferIndex]:nil;
 return [NSString stringWithFormat:@"attribute[%u] format=%lu(%s) bufferIndex=%lu offset=%lu stride=%lu stepFunction=%lu stepRate=%lu layoutPresent=%u",index,
  (unsigned long)a.format,fuxi::vertexFormatInfo(a.format).name,(unsigned long)a.bufferIndex,(unsigned long)a.offset,
  (unsigned long)l.stride,(unsigned long)l.stepFunction,(unsigned long)l.stepRate,l!=nil];
}
static NSString *vertexDescriptorText(MTLVertexDescriptor *vd) {
 // At most 31 public attributes/layouts; only configured entries, including
 // nondefault unused state, are emitted. No object descriptions or pointers.
 NSMutableString *text=[NSMutableString stringWithString:@"attributes={"];
 bool referenced[31]{};bool first=true;
 if(vd)for(unsigned i=0;i<31;++i){auto a=vd.attributes[i];
  if(a.format!=MTLVertexFormatInvalid && a.bufferIndex<31)referenced[a.bufferIndex]=true;
  if(a.format==MTLVertexFormatInvalid && !a.offset && !a.bufferIndex)continue;
  [text appendFormat:@"%s%@",first?"":";",vertexAttributeText(vd,i)];first=false;
 }
 [text appendString:@"} layouts={"];first=true;
 if(vd)for(unsigned i=0;i<31;++i){auto l=vd.layouts[i];
  if(!referenced[i] && !l.stride && l.stepFunction==MTLVertexStepFunctionPerVertex && l.stepRate==1)continue;
  [text appendFormat:@"%s%u:stride=%lu/stepFunction=%lu/stepRate=%lu",first?"":";",i,(unsigned long)l.stride,(unsigned long)l.stepFunction,(unsigned long)l.stepRate];first=false;
 }
 [text appendString:@"}"];return text;
}
static NSString *vertexDescriptorRejection(MTLVertexDescriptor *vd) {
 bool referenced[31]{};
 if(vd)for(unsigned i=0;i<31;++i){auto a=vd.attributes[i];
  if(a.format==MTLVertexFormatInvalid)continue;
  if(const char *why=fuxi::vertexAttributeRejection(i,a.format,a.bufferIndex,a.offset))
   return [NSString stringWithFormat:@"vertexDescriptor %@ reason=%s",vertexAttributeText(vd,i),why];
  referenced[a.bufferIndex]=true;
 }
 if(vd)for(unsigned i=0;i<31;++i){auto l=vd.layouts[i];
  if(!referenced[i] && !l.stride && l.stepFunction==MTLVertexStepFunctionPerVertex && l.stepRate==1)continue;
  if(const char *why=fuxi::vertexLayoutRejection(i,l.stride,l.stepFunction,l.stepRate,referenced[i])) {
   NSString *attribute=@"attribute=none";
   for(unsigned j=0;j<31;++j)if(vd.attributes[j].format!=MTLVertexFormatInvalid && vd.attributes[j].bufferIndex==i){attribute=vertexAttributeText(vd,j);break;}
   return [NSString stringWithFormat:@"vertexDescriptor layout[%u] stride=%lu stepFunction=%lu stepRate=%lu %@ reason=%s",i,(unsigned long)l.stride,(unsigned long)l.stepFunction,(unsigned long)l.stepRate,attribute,why];
  }
 }
 return nil;
}
static NSString *pipelineDescriptorText(MTLRenderPipelineDescriptor *d){
 NSMutableString *text=[NSMutableString stringWithFormat:@"pipeline samples=%lu depth=%lu stencil=%lu alphaCoverage=%u alphaOne=%u raster=%u topology=%lu %@ colors=[",(unsigned long)d.rasterSampleCount,(unsigned long)d.depthAttachmentPixelFormat,(unsigned long)d.stencilAttachmentPixelFormat,d.alphaToCoverageEnabled,d.alphaToOneEnabled,d.rasterizationEnabled,(unsigned long)d.inputPrimitiveTopology,vertexDescriptorText(d.vertexDescriptor)];
 for(unsigned i=0;i<8;++i){auto a=d.colorAttachments[i];[text appendFormat:@"%u:format=%lu blend=%u rgb=%lu,%lu,%lu alpha=%lu,%lu,%lu mask=%lu;",i,(unsigned long)a.pixelFormat,a.blendingEnabled,(unsigned long)a.sourceRGBBlendFactor,(unsigned long)a.destinationRGBBlendFactor,(unsigned long)a.rgbBlendOperation,(unsigned long)a.sourceAlphaBlendFactor,(unsigned long)a.destinationAlphaBlendFactor,(unsigned long)a.alphaBlendOperation,(unsigned long)a.writeMask];}
 [text appendString:@"]"];return text;
}
static NSString *pipelineRejection(MTLRenderPipelineDescriptor *d,id device) {
 id v=d.vertexFunction,f=d.fragmentFunction;MTLVertexDescriptor *vd=d.vertexDescriptor;
 bool vertex=[v isKindOfClass:[FuxiFunction class]],fragment=[f isKindOfClass:[FuxiFunction class]];
 int vs=vertex?((FuxiFunction *)v)->_stage:0,fs=fragment?((FuxiFunction *)f)->_stage:0;
 auto vl=d.vertexLinkedFunctions,fl=d.fragmentLinkedFunctions;
 char colors[640]{};size_t used=0;
 for(unsigned i=0;i<8;++i) {auto a=d.colorAttachments[i];used+=(size_t)snprintf(colors+used,sizeof(colors)-used,"%s%u:%lu/%u/%lu",i?",":"",i,(unsigned long)a.pixelFormat,a.blendingEnabled,(unsigned long)a.writeMask);}
 static std::atomic<unsigned> descriptorLogs{0};
 if(descriptorLogs.fetch_add(1)<64)fprintf(stderr,"FUXI MTL: pipeline descriptor present=%u vertex=%s/stage%d/device%u fragment=%s/stage%d/device%u vertexDescriptor=%u samples=%lu alphaCoverage=%u alphaOne=%u raster=%u depth=%lu stencil=%lu topology=%lu tessFormat=%lu tessMax=%lu indirect=%u amplification=%lu vertexLinked=%u/%lu/%lu/%lu/%lu fragmentLinked=%u/%lu/%lu/%lu/%lu colors(format/blend/mask)=%s %s\n",
  d!=nil,v?object_getClassName(v):"nil",vs,vertex && [v device]==device,f?object_getClassName(f):"nil",fs,fragment && [f device]==device,vd!=nil,
  (unsigned long)d.rasterSampleCount,d.alphaToCoverageEnabled,d.alphaToOneEnabled,d.rasterizationEnabled,(unsigned long)d.depthAttachmentPixelFormat,(unsigned long)d.stencilAttachmentPixelFormat,
  (unsigned long)d.inputPrimitiveTopology,(unsigned long)d.tessellationFactorFormat,(unsigned long)d.maxTessellationFactor,d.supportIndirectCommandBuffers,(unsigned long)d.maxVertexAmplificationCount,
  vl!=nil,(unsigned long)vl.functions.count,(unsigned long)vl.binaryFunctions.count,(unsigned long)vl.privateFunctions.count,(unsigned long)vl.groups.count,
  fl!=nil,(unsigned long)fl.functions.count,(unsigned long)fl.binaryFunctions.count,(unsigned long)fl.privateFunctions.count,(unsigned long)fl.groups.count,colors,vertexDescriptorText(vd).UTF8String);
#define CHECK(field,value,accepted) do {__typeof__(value) actual=(value);if(!stage4Value("pipeline",field,(uint64_t)actual,(accepted)))return [NSString stringWithUTF8String:field];}while(0)
 CHECK("descriptor",d,d!=nil);
 if(!stage4Class("pipeline","vertexFunction.class",v,[FuxiFunction class]))return @"vertexFunction.class";
 if(!stage4Class("pipeline","fragmentFunction.class",f,[FuxiFunction class]))return @"fragmentFunction.class";
 CHECK("vertexFunction.device",[v device],actual==device);
 CHECK("fragmentFunction.device",[f device],actual==device);
 CHECK("vertexFunction.stage",vs,actual==1);
 CHECK("fragmentFunction.stage",fs,actual==2);
 // 20H392's getter lazily creates an empty descriptor. Presence alone cannot
 // distinguish the untouched default from an explicitly supplied empty object.
 // ABI 6 transmits exact formats/offsets/buffer slots/strides, per-vertex only.
 if(NSString *why=vertexDescriptorRejection(vd))return why;
 CHECK("rasterSampleCount",d.rasterSampleCount,actual==1);
 CHECK("alphaToCoverageEnabled",d.alphaToCoverageEnabled,!actual);
 CHECK("alphaToOneEnabled",d.alphaToOneEnabled,!actual);
 CHECK("rasterizationEnabled",d.rasterizationEnabled,actual);
 CHECK("depthAttachmentPixelFormat",d.depthAttachmentPixelFormat,actual==MTLPixelFormatInvalid);
 CHECK("stencilAttachmentPixelFormat",d.stencilAttachmentPixelFormat,actual==MTLPixelFormatInvalid);
 CHECK("inputPrimitiveTopology",d.inputPrimitiveTopology,actual==MTLPrimitiveTopologyClassUnspecified || actual==MTLPrimitiveTopologyClassTriangle);
 CHECK("maxVertexAmplificationCount",d.maxVertexAmplificationCount,actual==1);
 // Tessellation settings have no effect without a patch draw; indirect support
 // is a capability request, not state for our sole direct draw. Do not gate them.
#define LINKED(prefix,obj) CHECK(prefix ".functions",obj.functions.count,!actual);CHECK(prefix ".binaryFunctions",obj.binaryFunctions.count,!actual);CHECK(prefix ".privateFunctions",obj.privateFunctions.count,!actual);CHECK(prefix ".groups",obj.groups.count,!actual)
 LINKED("vertexLinkedFunctions",vl);LINKED("fragmentLinkedFunctions",fl);
#undef LINKED
 bool anyColor=false;for(unsigned i=0;i<8;++i)anyColor|=d.colorAttachments[i].pixelFormat!=MTLPixelFormatInvalid;CHECK("colorAttachments.empty",anyColor,actual);
 for(unsigned i=0;i<8;++i) {
  auto a=d.colorAttachments[i];char field[96];
  snprintf(field,sizeof(field),"colorAttachments[%u].pixelFormat",i);CHECK(field,a.pixelFormat,actual==MTLPixelFormatInvalid || actual==80 || actual==70 || actual==115 || actual==10 || actual==30 || actual==94 || actual==43);
  // Blend and write-mask state on an absent target cannot affect this pipeline.
  if(a.pixelFormat!=MTLPixelFormatInvalid) {snprintf(field,sizeof(field),"colorAttachments[%u].writeMask",i);CHECK(field,a.writeMask,!(actual&~15u));
   if(a.blendingEnabled && (a.sourceRGBBlendFactor>MTLBlendFactorOneMinusDestinationAlpha || a.destinationRGBBlendFactor>MTLBlendFactorOneMinusDestinationAlpha || a.sourceAlphaBlendFactor>MTLBlendFactorOneMinusDestinationAlpha || a.destinationAlphaBlendFactor>MTLBlendFactorOneMinusDestinationAlpha || a.rgbBlendOperation>MTLBlendOperationMax || a.alphaBlendOperation>MTLBlendOperationMax)){failureLog("FUXI MTL: pipeline reject blend attachment[%u]\n",i);return @"blend factor/operation";}}
 }
#undef CHECK
 return nil;
}
static bool identitySwizzle(MTLTextureSwizzleChannels s) {
 return s.red==MTLTextureSwizzleRed && s.green==MTLTextureSwizzleGreen && s.blue==MTLTextureSwizzleBlue && s.alpha==MTLTextureSwizzleAlpha;
}
// One line contains every public descriptor field relevant to our profile.
static NSString *textureDescriptorText(MTLTextureDescriptor *d) {
 if(!d)return @"nil";
 auto w=d.swizzle;
 return [NSString stringWithFormat:@"type=%lu format=%lu size=%lux%lux%lu mips=%lu array=%lu samples=%lu storage=%lu cache=%lu hazard=%lu usage=0x%lx swizzle=%lu,%lu,%lu,%lu optimized=%u resources=0x%lx",
  (unsigned long)d.textureType,(unsigned long)d.pixelFormat,(unsigned long)d.width,(unsigned long)d.height,(unsigned long)d.depth,
  (unsigned long)d.mipmapLevelCount,(unsigned long)d.arrayLength,(unsigned long)d.sampleCount,(unsigned long)d.storageMode,
  (unsigned long)d.cpuCacheMode,(unsigned long)d.hazardTrackingMode,(unsigned long)d.usage,
  (unsigned long)w.red,(unsigned long)w.green,(unsigned long)w.blue,(unsigned long)w.alpha,d.allowGPUOptimizedContents,(unsigned long)d.resourceOptions];
}
static bool textureValue(MTLTextureDescriptor *d,const char *field,uint64_t value,bool accepted) {
 if(!accepted)failureLog("FUXI MTL: texture reject %s %llu desc=%s\n",field,(unsigned long long)value,textureDescriptorText(d).UTF8String);
 return accepted;
}
static bool textureDescriptorAccepted(MTLTextureDescriptor *d,uint32_t dimension) {
#define CHECK(field,value,accepted) do {__typeof__(value) actual=(value);if(!textureValue(d,field,(uint64_t)actual,(accepted)))return false;}while(0)
 CHECK("descriptor",d,actual!=nil);
 CHECK("textureType",d.textureType,(actual==MTLTextureType2D || actual==MTLTextureType1D || actual==MTLTextureType3D));
 CHECK("textureType.shapeUsage",d.textureType,fuxi::textureShapeAccepted((uint32_t)actual,(uint32_t)d.height,(uint32_t)d.usage,(uint32_t)d.depth,(uint32_t)d.mipmapLevelCount));
 CHECK("pixelFormat",d.pixelFormat,fuxi::textureFormatAccepted((uint32_t)actual));
 CHECK("width",d.width,actual && actual<=dimension);CHECK("height",d.height,actual && actual<=dimension);CHECK("depth",d.depth,actual && (d.textureType==MTLTextureType3D || actual==1) && actual<=dimension);
 CHECK("mipmapLevelCount",d.mipmapLevelCount,actual && actual<=fuxi::mipCount((uint32_t)d.width,(uint32_t)d.height));CHECK("arrayLength",d.arrayLength,actual==1);CHECK("sampleCount",d.sampleCount,actual==1);CHECK("swizzle.identity",identitySwizzle(d.swizzle),actual);
 CHECK("storageMode",d.storageMode,actual==MTLStorageModePrivate || actual==MTLStorageModeShared || actual==MTLStorageModeMemoryless);CHECK("cpuCacheMode",d.cpuCacheMode,actual==MTLCPUCacheModeDefaultCache || actual==MTLCPUCacheModeWriteCombined);
 CHECK("hazardTrackingMode",d.hazardTrackingMode,actual==MTLHazardTrackingModeDefault || actual==MTLHazardTrackingModeTracked);
 CHECK("usage",d.usage,!(actual&~(MTLTextureUsageRenderTarget|MTLTextureUsageShaderRead|MTLTextureUsagePixelFormatView)));
 CHECK("pixelFormat.A8RenderTarget",d.pixelFormat,actual!=MTLPixelFormatA8Unorm || (!(d.usage&MTLTextureUsageRenderTarget) && d.usage!=MTLTextureUsageUnknown));
 // allowGPUOptimizedContents is an implementation hint, not pixel semantics.
#undef CHECK
 return true;
}
@interface FuxiTexture : FuxiResource { @public fuxi::Texture *_texture; MTLTextureUsage _usage; IOSurfaceRef _surface; BOOL _shared,_memoryless,_surfaceLockHeld;FuxiTexture *_viewParent;FuxiBuffer *_backingBuffer;NSUInteger _backingOffset,_backingRow;uint32_t _baseLevel,_levelCount;MTLCPUCacheMode _cacheMode;NSString *_descriptorText; }
@end
@implementation FuxiTexture
- (void)rejectSelector:(SEL)selector {
 if(clientMode==fuxi::ClientMode::census){censusLog(self,selector,false);return;}
 failureLog("FUXI MTL: texture reject selector 0 desc=%s selector=%.160s\n",(_descriptorText?_descriptorText:@"unavailable").UTF8String,sel_getName(selector));
}
- (void)dealloc {
 bool safeToUnlock=_viewParent?true:owner(_device)->backend->destroy(_texture);
 // A timeout leaves ownership unknown. Preserve the locked surface/pages until
 // process teardown, just as Backend preserves Vulkan allocations on poison.
 if(_surface && safeToUnlock) {if(_surfaceLockHeld)IOSurfaceUnlock(_surface,0,nullptr);IOSurfaceDecrementUseCount(_surface);CFRelease(_surface);}
 [_descriptorText release];[_viewParent release];[_backingBuffer release];[_device release];[super dealloc];
}
- (id)device {CENSUS();return _device;}
- (NSUInteger)width {CENSUS();return _texture->levelWidth(_baseLevel);}
- (NSUInteger)height {CENSUS();return _texture->levelHeight(_baseLevel);}
- (NSUInteger)depth {CENSUS();return _texture->levelDepth(_baseLevel);}
- (NSUInteger)arrayLength {CENSUS();return 1;}
- (NSUInteger)protectionOptions {CENSUS();return 0;}
- (NSUInteger)mipmapLevelCount {CENSUS();return _levelCount?_levelCount:_texture->levels;}
- (NSUInteger)sampleCount {CENSUS();return 1;}
- (MTLTextureType)textureType {CENSUS();return (MTLTextureType)_texture->textureType;}
- (MTLPixelFormat)pixelFormat {CENSUS();return (MTLPixelFormat)_texture->format;}
// Memoryless is only a promise not to load or store, so private memory backs it.
- (MTLStorageMode)storageMode {CENSUS();return _memoryless?MTLStorageModeMemoryless:(_surface || _shared)?MTLStorageModeShared:MTLStorageModePrivate;}
- (MTLTextureUsage)usage {CENSUS();return _usage;}
- (IOSurfaceRef)iosurface {CENSUS();return _viewParent?[_viewParent iosurface]:_surface;}
- (NSUInteger)iosurfacePlane {CENSUS();return 0;}
- (MTLTextureSwizzleChannels)swizzle {CENSUS();return MTLTextureSwizzleChannelsMake(MTLTextureSwizzleRed,MTLTextureSwizzleGreen,MTLTextureSwizzleBlue,MTLTextureSwizzleAlpha);}
- (MTLCPUCacheMode)cpuCacheMode {CENSUS();return _cacheMode;}
- (MTLHazardTrackingMode)hazardTrackingMode {CENSUS();return MTLHazardTrackingModeTracked;}
- (NSUInteger)allocatedSize {CENSUS();return _texture->allocated;}
- (MTLPurgeableState)setPurgeableState:(MTLPurgeableState)state {CENSUS();
 if(state<MTLPurgeableStateKeepCurrent || state>MTLPurgeableStateEmpty){unsupported(self,_cmd);return (MTLPurgeableState)0;}
 if(_viewParent || _parentHeap)return MTLPurgeableStateNonVolatile;
 if(_backingBuffer)return [_backingBuffer setPurgeableState:state];
 if(state==MTLPurgeableStateEmpty){std::lock_guard<std::mutex> lock(owner(_device)->backend->mutex);_texture->defined=false;_texture->definedLevels=0;}
 return [super setPurgeableState:state];
}
- (BOOL)isFramebufferOnly {CENSUS();return NO;}
- (MTLResourceOptions)resourceOptions {CENSUS();return (MTLResourceOptions)(([self storageMode]<<MTLResourceStorageModeShift)|(_cacheMode<<MTLResourceCPUCacheModeShift));}
- (id)parentTexture {CENSUS();return _viewParent;}
- (NSUInteger)parentRelativeLevel {CENSUS();return _viewParent?_baseLevel-_viewParent->_baseLevel:0;}
- (NSUInteger)parentRelativeSlice {CENSUS();return 0;}
- (id)buffer {CENSUS();return _viewParent?[_viewParent buffer]:_backingBuffer;}
- (NSUInteger)bufferOffset {CENSUS();return _viewParent?[_viewParent bufferOffset]:_backingOffset;}
- (NSUInteger)bufferBytesPerRow {CENSUS();return _viewParent?[_viewParent bufferBytesPerRow]:_backingRow;}
- (id)newTextureViewWithPixelFormat:(MTLPixelFormat)format {CENSUS();
 return [self newTextureViewWithPixelFormat:format textureType:[self textureType] levels:NSMakeRange(0,[self mipmapLevelCount]) slices:NSMakeRange(0,1)];
}
- (id)newTextureViewWithPixelFormat:(MTLPixelFormat)format textureType:(MTLTextureType)type levels:(NSRange)levels slices:(NSRange)slices {CENSUS();
 if(format!=[self pixelFormat] || type!=[self textureType] || !levels.length || levels.location>=[self mipmapLevelCount] || levels.length>[self mipmapLevelCount]-levels.location || slices.location || slices.length!=1){
  failureLog("FUXI MTL: texture reject viewRange 0 format=%lu type=%lu levels=%lu+%lu slices=%lu+%lu parent=format%lu/mips%lu desc=%s\n",(unsigned long)format,(unsigned long)type,(unsigned long)levels.location,(unsigned long)levels.length,(unsigned long)slices.location,(unsigned long)slices.length,(unsigned long)[self pixelFormat],(unsigned long)[self mipmapLevelCount],(_descriptorText?_descriptorText:@"unavailable").UTF8String);return nil;
 }
 FuxiTexture *view=[FuxiTexture new];view->_device=[_device retain];view->_texture=_texture;view->_baseLevel=_baseLevel+(uint32_t)levels.location;view->_levelCount=(uint32_t)levels.length;
 view->_descriptorText=[_descriptorText copy];view->_usage=_usage;view->_shared=[self storageMode]==MTLStorageModeShared;view->_memoryless=_memoryless;view->_cacheMode=_cacheMode;view->_viewParent=[self retain];return view;
}
- (void)transfer:(void *)bytes row:(NSUInteger)row image:(NSUInteger)image region:(MTLRegion)r level:(NSUInteger)level slice:(NSUInteger)slice write:(BOOL)write {CENSUS();
 if([self storageMode]!=MTLStorageModeShared)invalid(@"texture CPU transfer requires shared storage");
 if(level>=[self mipmapLevelCount])invalid(@"texture CPU mip level");
 uint32_t absolute=_baseLevel+(uint32_t)level;
 size_t pixel=fuxi::Backend::texelBytes((uint32_t)[self pixelFormat]),width=_texture->levelWidth(absolute),height=_texture->levelHeight(absolute);
 bool volume=_texture->textureType==7;size_t depth=_texture->levelDepth(absolute);
 if(!bytes || slice || (!volume && (r.origin.z || r.size.depth!=1)) || !r.size.depth || r.origin.z>depth || r.size.depth>depth-r.origin.z || !r.size.width || !r.size.height ||
    r.origin.x>width || r.size.width>width-r.origin.x || r.origin.y>height || r.size.height>height-r.origin.y ||
    row<r.size.width*pixel || row>SIZE_MAX/r.size.height || (image && image<row*r.size.height) || (r.size.depth>1 && image>SIZE_MAX/r.size.depth))invalid(@"texture CPU region/stride");
 auto *backend=owner(_device)->backend;fuxi::Buffer *staging=nullptr;
 try {
  if(gpuScanout)backend->requireLive();
  fuxi::SurfaceLease lease(gpuScanout,lockScanoutSurface,unlockScanoutSurface,&backend->poisoned);
  if(gpuScanout)lease.add((uintptr_t)[self iosurface]);
  // Shared buffer textures and their views expose the same CPU rows. The GPU
  // shadow is refreshed at use; CPU writes must precede commit, never race it.
  if(FuxiBuffer *backing=(FuxiBuffer *)[self buffer]){
   std::lock_guard<std::mutex> guard(backend->mutex);backend->requireLive();
   auto *base=(uint8_t *)(backing->_shadow && [self usage]==MTLTextureUsageShaderRead?backing->_wrapped:backing->_buffer->contents)+[self bufferOffset]+r.origin.y*[self bufferBytesPerRow]+r.origin.x*pixel;
   for(size_t y=0;y<r.size.height;++y){auto *cpu=(uint8_t *)bytes+y*row;auto *buffer=base+y*[self bufferBytesPerRow];
    if(write)memmove(buffer,cpu,r.size.width*pixel);else memmove(cpu,buffer,r.size.width*pixel);}
   return;
  }
  staging=backend->allocate(width*height*depth*pixel);fuxi::Operation op{};op.texture=_texture;op.srcLevel=op.dstLevel=absolute;
  // Staging is the whole level, rows then slices tightly packed; the caller's slices are image bytes apart.
  size_t sliceStride=image?image:row*r.size.height;
  bool full=!r.origin.x && !r.origin.y && !r.origin.z && r.size.width==width && r.size.height==height && r.size.depth==depth;
  if(!write || (!full && _texture->isDefined(absolute))){op.dst=staging;backend->submit({op});}
  if(write){for(size_t z=0;z<r.size.depth;++z)for(size_t y=0;y<r.size.height;++y)memcpy((uint8_t *)staging->contents+(((r.origin.z+z)*height+r.origin.y+y)*width+r.origin.x)*pixel,(uint8_t *)bytes+z*sliceStride+y*row,r.size.width*pixel);
   op={};op.texture=_texture;op.srcLevel=op.dstLevel=absolute;op.src=staging;op.upload=true;backend->submit({op});
  }else for(size_t z=0;z<r.size.depth;++z)for(size_t y=0;y<r.size.height;++y)memcpy((uint8_t *)bytes+z*sliceStride+y*row,(uint8_t *)staging->contents+(((r.origin.z+z)*height+r.origin.y+y)*width+r.origin.x)*pixel,r.size.width*pixel);
  backend->destroy(staging);
 }catch(const std::exception &e){backend->destroy(staging);invalid([NSString stringWithUTF8String:e.what()]);}
}
- (void)replaceRegion:(MTLRegion)r mipmapLevel:(NSUInteger)level slice:(NSUInteger)slice withBytes:(const void *)bytes bytesPerRow:(NSUInteger)row bytesPerImage:(NSUInteger)image {
 if(caTrace())fprintf(stderr,"FUXI MTL: trace replaceRegion %lu,%lu %lux%lu row %lu format %lu\n",(unsigned long)r.origin.x,(unsigned long)r.origin.y,(unsigned long)r.size.width,(unsigned long)r.size.height,(unsigned long)row,(unsigned long)[self pixelFormat]);
 [self transfer:const_cast<void *>(bytes) row:row image:image region:r level:level slice:slice write:YES];
}
- (void)replaceRegion:(MTLRegion)r mipmapLevel:(NSUInteger)level withBytes:(const void *)bytes bytesPerRow:(NSUInteger)row {
 [self replaceRegion:r mipmapLevel:level slice:0 withBytes:bytes bytesPerRow:row bytesPerImage:0];
}
- (void)getBytes:(void *)bytes bytesPerRow:(NSUInteger)row bytesPerImage:(NSUInteger)image fromRegion:(MTLRegion)r mipmapLevel:(NSUInteger)level slice:(NSUInteger)slice {
 [self transfer:bytes row:row image:image region:r level:level slice:slice write:NO];
}
- (void)getBytes:(void *)bytes bytesPerRow:(NSUInteger)row fromRegion:(MTLRegion)r mipmapLevel:(NSUInteger)level {
 [self getBytes:bytes bytesPerRow:row bytesPerImage:0 fromRegion:r mipmapLevel:level slice:0];
}
@end
@implementation FuxiBuffer (CATextures)
- (id)newLinearTextureWithDescriptor:(MTLTextureDescriptor *)d offset:(NSUInteger)offset bytesPerRow:(NSUInteger)row bytesPerImage:(NSUInteger)image {CENSUS();FUXI_PHASE(newTexture);
 if(caTrace() && d)fprintf(stderr,"FUXI MTL: trace linearTexture %lux%lu offset %lu row %lu image %lu format %lu usage %lu length %lu\n",(unsigned long)d.width,(unsigned long)d.height,(unsigned long)offset,(unsigned long)row,(unsigned long)image,(unsigned long)d.pixelFormat,(unsigned long)d.usage,(unsigned long)_wrappedLength);
 if(!textureDescriptorAccepted(d,owner(_device)->backend->maxTextureDimension()))return nil;
#define CHECK(field,value,accepted) do {__typeof__(value) actual=(value);if(!textureValue(d,field,(uint64_t)actual,(accepted)))return nil;}while(0)
 CHECK("linear.storageMode",d.storageMode,actual==MTLStorageModeShared);
 CHECK("linear.mipmapLevelCount",d.mipmapLevelCount,actual==1);
 CHECK("linear.bytesPerRow",row,actual && d.height<=SIZE_MAX/actual);
 CHECK("linear.bytesPerImage",image,fuxi::linearImageFits(actual,row,d.width,d.height,(uint32_t)d.pixelFormat));
#undef CHECK
 FuxiTexture *t=[FuxiTexture new];t->_descriptorText=[textureDescriptorText(d) copy];t->_cacheMode=d.cpuCacheMode;t->_device=[_device retain];t->_usage=d.usage;t->_shared=YES;
 if(d.textureType!=MTLTextureType2D){failureLog("FUXI MTL: texture reject linearTextureType %lu desc=%s\n",(unsigned long)d.textureType,textureDescriptorText(d).UTF8String);[t release];return nil;}
 try {t->_texture=owner(_device)->backend->linearTexture(_buffer,(uint32_t)d.pixelFormat,(uint32_t)d.usage,(uint32_t)d.width,(uint32_t)d.height,offset,row,_shadow==YES);}
 catch(const std::exception &e){failureLog("FUXI MTL: texture reject linearAllocation 0 desc=%s offset=%lu row=%lu image=%lu reason=%s\n",textureDescriptorText(d).UTF8String,(unsigned long)offset,(unsigned long)row,(unsigned long)image,e.what());[t release];return nil;}
 t->_backingBuffer=[self retain];t->_backingOffset=offset;t->_backingRow=row;return t;
}
- (id)newTextureWithDescriptor:(MTLTextureDescriptor *)d offset:(NSUInteger)offset bytesPerRow:(NSUInteger)row {
 return [self newLinearTextureWithDescriptor:d offset:offset bytesPerRow:row bytesPerImage:0];
}
@end
@interface FuxiPipeline : FuxiLabeledObject { @public id _device; fuxi::Pipeline *_pipeline;NSString *_descriptorText; }
@end
@implementation FuxiPipeline
- (void)dealloc {[_descriptorText release];if(_pipeline)owner(_device)->backend->destroy(_pipeline);[_device release];[super dealloc];}
- (id)device {CENSUS();return _device;}
@end
// Survey mode: CA aborts on a nil render pipeline ("Metal failed to build render
// pipeline", device M9l survey boot). After the refusal is recorded, hand back an
// empty pipeline; its draws are dropped and every submission is already a no-op.
static id surveyPipeline(id refused,NSError **error,id device){
 if(refused || !backboardSurvey.active() || !fuxi::backboardGate.blocked())return refused;
 if(error)*error=nil;FuxiPipeline *p=[FuxiPipeline new];p->_device=[device retain];p->_descriptorText=@"survey placeholder";return p;
}
@interface FuxiSampler : FuxiLabeledObject { @public id _device;fuxi::Sampler *_sampler; }
@end
@implementation FuxiSampler
- (MTLPixelFormat)pixelFormat {CENSUS();return MTLPixelFormatInvalid;}
- (void)dealloc {owner(_device)->backend->destroy(_sampler);[_device release];[super dealloc];}
- (id)device {CENSUS();return _device;}
@end
// Only the neutral state is representable by our attachment-free backend.
@interface FuxiDepthStencil : FuxiLabeledObject { @public id _device; }
@end
@implementation FuxiDepthStencil
- (void)dealloc {[_device release];[super dealloc];}
- (id)device {CENSUS();return _device;}
@end
// Automatic shared/private heaps are a resource budget; Vulkan allocations remain
// independent and nonaliasable. Placement/sparse heaps are not advertised.
@interface FuxiHeap : FuxiResource <FuxiHeapBudget> { @public NSUInteger _size,_used;MTLStorageMode _storage;MTLCPUCacheMode _cacheMode; }
@end
@implementation FuxiHeap
- (MTLPurgeableState)setPurgeableState:(MTLPurgeableState)state {CENSUS();if(state<MTLPurgeableStateKeepCurrent || state>MTLPurgeableStateEmpty){unsupported(self,_cmd);return (MTLPurgeableState)0;}return MTLPurgeableStateNonVolatile;}
- (void)dealloc {[_device release];[super dealloc];}
- (NSUInteger)size {CENSUS();return _size;}
- (NSUInteger)usedSize {CENSUS();@synchronized(self){return _used;}}
- (NSUInteger)currentAllocatedSize {CENSUS();return [self usedSize];}
- (MTLStorageMode)storageMode {CENSUS();return _storage;}
- (MTLCPUCacheMode)cpuCacheMode {CENSUS();return _cacheMode;}
- (MTLHazardTrackingMode)hazardTrackingMode {CENSUS();return MTLHazardTrackingModeTracked;}
- (MTLResourceOptions)resourceOptions {CENSUS();return (MTLResourceOptions)((_storage<<MTLResourceStorageModeShift)|(_cacheMode<<MTLResourceCPUCacheModeShift));}
- (MTLHeapType)type {CENSUS();return MTLHeapTypeAutomatic;}
- (NSUInteger)maxAvailableSizeWithAlignment:(NSUInteger)alignment {CENSUS();
 if(!alignment || (alignment&(alignment-1)))return 0;@synchronized(self){return (_size-_used)&~(alignment-1);}
}
- (void)fuxiRelinquishBytes:(NSUInteger)bytes {@synchronized(self){_used-=bytes;}}
- (id)adopt:(FuxiResource *)resource bytes:(NSUInteger)bytes {
 if(!resource)return nil;@synchronized(self){if(bytes>_size-_used){[resource release];return nil;}
 _used+=bytes;resource->_parentHeap=[self retain];resource->_heapBytes=bytes;}return resource;
}
- (id)newBufferWithLength:(NSUInteger)length options:(MTLResourceOptions)options {CENSUS();
 if(options!=[self resourceOptions] || length>[self maxAvailableSizeWithAlignment:1])return nil;
 FuxiBuffer *b=[_device newBufferWithLength:length options:options];return [self adopt:b bytes:b?b->_buffer->allocated:0];
}
- (id)newTextureWithDescriptor:(MTLTextureDescriptor *)descriptor {CENSUS();
 if(!descriptor)return nil;
 if(!textureValue(descriptor,"heap.storageMode",descriptor.storageMode,descriptor.storageMode==_storage) || !textureValue(descriptor,"heap.cpuCacheMode",descriptor.cpuCacheMode,descriptor.cpuCacheMode==_cacheMode))return nil;
 FuxiTexture *t=[_device newTextureWithDescriptor:descriptor];return [self adopt:t bytes:t?t->_texture->allocated:0];
}
@end
// MIT NativeMetal NBSharedEvent listener forwarding, adapted to MRC and our
// synchronous completion. No shared event handle is fabricated.
@interface FuxiEventNotice : NSObject { @public dispatch_queue_t _queue;MTLSharedEventNotificationBlock _block;uint64_t _value; }
@end
@implementation FuxiEventNotice
- (void)dealloc {if(_queue)dispatch_release(_queue);[_block release];[super dealloc];}
@end
@interface FuxiEvent : FuxiLabeledObject { @public id _device;fuxi::Timeline _timeline;NSMutableArray *_notices;BOOL _shared; }
@end
@implementation FuxiEvent
- (id)device {CENSUS();return _device;}
- (void)dealloc {[_device release];[_notices release];[super dealloc];}
- (uint64_t)signaledValue {CENSUS();return _timeline.value();}
- (void)setSignaledValue:(uint64_t)value {CENSUS();
 @synchronized(self){_timeline.signal(value);uint64_t actual=_timeline.value();NSArray *notices=[_notices copy];
  for(FuxiEventNotice *notice in notices)if(notice->_value<=actual){
   [_notices removeObjectIdenticalTo:notice];dispatch_async(notice->_queue,^{notice->_block((id)self,actual);});
  }
  [notices release];}
}
- (BOOL)waitUntilSignaledValue:(uint64_t)value timeoutMS:(uint64_t)milliseconds {CENSUS();return _timeline.wait(value,milliseconds);}
- (void)notifyListener:(MTLSharedEventListener *)listener atValue:(uint64_t)value block:(MTLSharedEventNotificationBlock)block {CENSUS();
 if(!_shared || !listener || !block){unsupported(self,_cmd);return;}
 FuxiEventNotice *notice=[FuxiEventNotice new];notice->_queue=listener.dispatchQueue?:dispatch_get_global_queue(QOS_CLASS_DEFAULT,0);dispatch_retain(notice->_queue);notice->_block=[block copy];notice->_value=value;
 @synchronized(self){if(!_notices)_notices=[NSMutableArray new];
  if(_notices.count>=256){[notice release];unsupported(self,_cmd);return;}
  [_notices addObject:notice];[self setSignaledValue:_timeline.value()];}
 [notice release];
}
- (id)newSharedEventHandle {CENSUS();unsupported(self,_cmd);return nil;}
@end
@interface FuxiSharedEvent : FuxiEvent
@end
@implementation FuxiSharedEvent
@end
@interface FuxiFence : FuxiLabeledObject { @public id _device; }
@end
@implementation FuxiFence
- (id)device {CENSUS();return _device;}
- (void)dealloc {[_device release];[super dealloc];}
@end
struct FuxiEventPoint {size_t operation;FuxiEvent *event;uint64_t value;bool signal;};
@class FuxiCommandBuffer;
static thread_local FuxiCommandBuffer *completionCommand;
@interface FuxiQueue : FuxiLabeledObject { @public fuxi::CommandStorage<fuxi::Operation> _operationStorage;fuxi::UploadStorage<FuxiBuffer *> _uploadStorage;fuxi::ShadowPool _shadowPool;id _device;dispatch_queue_t _completionQueue,_submitQueue,_retireQueue,_finishQueue,_deliveryQueue;dispatch_semaphore_t _inflight;std::atomic<unsigned> _depth,_pending;std::atomic<uint64_t> _traceReturn,_traceDelivery,_traceHandlers,_traceWaitCompleted,_traceWaitScheduled; }
@end
@interface FuxiCommandBuffer : FuxiLabeledObject {
 @public FuxiQueue *_queue; NSMutableArray *_resources,*_handlers,*_scheduledHandlers; NSError *_error;dispatch_group_t _completionGroup;
 dispatch_group_t _scheduledGroup,_commitReady;BOOL _overlap,_finished,_earlySubmit;
 fuxi::FrameSample _flightTrace;fuxi::FrameClock::time_point _commitStart,_commitReturn;uint64_t _gpuBefore;bool _surfaceFrame;
 std::function<void()> _retirement;std::shared_ptr<fuxi::SurfaceLease> _lease,_pendingLease;
 struct ShadowUse {FuxiBuffer *buffer;fuxi::ShadowRanges ranges;uint32_t textureUses=0;ShadowUse(FuxiBuffer *b):buffer(b),ranges(b->_wrappedLength){}};
 std::unordered_map<fuxi::Buffer *,ShadowUse> _shadowUses;fuxi::ShadowSnapshot _shadows;
 std::unordered_set<void *> _resourceSet,_sampledRes,_otherRes;fuxi::CommandResourceCache _resourceCache;
 fuxi::FrontendTimes _frontendTimes;
 BOOL _frameTrace;fuxi::FrameClock::time_point _traceCreated;fuxi::FrameCounts _traceCounts;std::thread::id _traceThread;
 fuxi::UploadRing<FuxiBuffer *> _uploads;
 uint64_t _renderEncoderID;std::vector<FuxiEventPoint> _events;std::vector<fuxi::Operation> _ops; MTLCommandBufferStatus _status; BOOL _encoding,_rejected,_enqueued,_scheduled;
}
@end
@interface FuxiBlit : FuxiLabeledObject { @public FuxiCommandBuffer *_command; BOOL _ended; }
@end
@interface FuxiRender : FuxiLabeledObject { @public FuxiCommandBuffer *_command; FuxiTexture *_target;FuxiTexture *_targets[7];fuxi::LazyText _passText; FuxiPipeline *_pipeline; MTLClearColor _clear; BOOL _ended,_drawn;
 FuxiTexture *_samples[fuxi::resourceSlots];FuxiSampler *_samplers[fuxi::resourceSlots];uint32_t _load,_store;size_t _lastOp;
 fuxi::Operation _state; }
@end
// A refused pass still yields an object: RenderBox caches encoder IMPs with methodForSelector:, which is NULL on nil and crashes the caller.
// Every selector forwards to rejectSelector:, so the command buffer stays rejected and commits with an error.
@interface FuxiRefusedRender : FuxiLabeledObject { @public FuxiCommandBuffer *_command; }
@end
// Preserve first-use ordering and command-buffer ownership, retaining each
// object once. Commit shadow refresh/scanout leasing then visits each once too.
static void retainCommandResource(FuxiCommandBuffer *command,id resource,bool bounded=false,fuxi::ShadowFullReason reason=fuxi::ShadowFullReason::other,bool sampledOnly=false){
 if(gpuScanout && resource)(sampledOnly?command->_sampledRes:command->_otherRes).insert((void *)resource); // Role per resource; unmarked means writer.
 if(command->_resourceCache.seen((void *)resource,bounded,reason))return;
 FuxiBuffer *buffer=nil;if([resource isKindOfClass:[FuxiBuffer class]])buffer=resource;else if([resource isKindOfClass:[FuxiTexture class]])buffer=[(FuxiTexture *)resource buffer];
 if(buffer && buffer->_shadow){auto &use=command->_shadowUses.try_emplace(buffer->_buffer,buffer).first->second;if(!bounded)use.ranges.full(reason);if(!bounded && [resource isKindOfClass:[FuxiTexture class]])use.textureUses|=4;}
 if(resource && command->_resourceSet.insert((void *)resource).second)[command->_resources addObject:resource];
}
static void retainCommandTexture(FuxiCommandBuffer *command,FuxiTexture *texture,bool sampled){
 retainCommandResource(command,texture,true,fuxi::ShadowFullReason::other,sampled);FuxiBuffer *buffer=(FuxiBuffer *)[texture buffer];if(!buffer || !buffer->_shadow)return;
 auto &use=command->_shadowUses.at(buffer->_buffer);use.textureUses|=sampled?1:2;
 if(sampled && [texture usage]==MTLTextureUsageShaderRead && [texture mipmapLevelCount]==1 && [texture textureType]==MTLTextureType2D)
  use.ranges.texture([texture bufferOffset],[texture bufferBytesPerRow],[texture width],[texture height],fuxi::Backend::texelBytes((uint32_t)[texture pixelFormat]));
 else use.ranges.full();
}
// IOSurfaces this command touches, sorted. A surface is a reader only when every texture on it was solely sampled by draws.
static std::vector<std::pair<uintptr_t,bool>> commandSurfaces(FuxiCommandBuffer *command){
 std::vector<std::pair<uintptr_t,bool>> out;
 for(id r in command->_resources){if(![r isKindOfClass:[FuxiTexture class]])continue;
  uintptr_t surface=(uintptr_t)[(FuxiTexture *)r iosurface];if(!surface)continue;
  bool write=!command->_sampledRes.count((void *)r) || command->_otherRes.count((void *)r);out.push_back({surface,write});}
 std::sort(out.begin(),out.end());
 size_t n=0;for(size_t i=0;i<out.size();++i){if(n && out[n-1].first==out[i].first)out[n-1].second|=out[i].second;else out[n++]=out[i];}
 out.resize(n);return out;
}
static void trackShadowDraw(FuxiCommandBuffer *command,const fuxi::Operation &op,fuxi::ShadowIndexCache &cache,uint64_t &scanned){
 fuxi::ShadowIndexRange indices;bool inspected=false;
 for(unsigned stage=0;stage<2;++stage)for(unsigned slot=0;slot<fuxi::resourceSlots;++slot){auto b=op.buffers[stage][slot];if(!b.buffer || b.bytes)continue;auto it=command->_shadowUses.find(b.buffer);if(it==command->_shadowUses.end())continue;
  auto &ranges=it->second.ranges;ranges.binding(op.pipeline->shadowWindows[stage][slot],b.offset);
  if(!stage)for(auto a:op.pipeline->options.attributes)if(a.format && a.buffer==slot){
   if(!op.indices)ranges.vertex(b.offset,op.first,op.vertices,false,op.pipeline->options.strides[slot],a.offset,a.format);
   else {if(!inspected){auto bytes=command->_shadows.view(op.indices->contents,op.indexOffset,uint64_t(op.vertices)*op.indexWidth);indices=cache.scan(op.indices,bytes,op.indexOffset,op.vertices,op.indexWidth,fuxi::primitiveRestart(op.topology),scanned);inspected=true;}
    ranges.vertexSpan(b.offset,indices,op.pipeline->options.strides[slot],a.offset,a.format);}
  }
 }
}
static void prepareCommandShadows(FuxiCommandBuffer *command,fuxi::FrameSample *trace){
 if(command->_shadowUses.empty())return;
 std::unordered_map<fuxi::Buffer *,fuxi::ShadowRanges> indices;uint64_t budget=0;
 for(const auto &op:command->_ops)if(op.pipeline && op.indices){auto it=command->_shadowUses.find(op.indices);if(it==command->_shadowUses.end() || it->second.ranges.isFull())continue;
  if(!fuxi::shadowIndexBytes(op.indices->length,op.indexOffset,op.vertices,op.indexWidth)){it->second.ranges.full(fuxi::ShadowFullReason::bounds);continue;}
  if(op.vertices>fuxi::ShadowIndexCache::drawLimit || op.vertices>fuxi::ShadowIndexCache::frameLimit-budget)continue;
  if(std::any_of(command->_ops.begin(),command->_ops.end(),[&](const auto &write){return write.dst==op.indices;}))continue;
  budget+=op.vertices;indices.try_emplace(op.indices,op.indices->length).first->second.add(op.indexOffset,uint64_t(op.vertices)*op.indexWidth);
 }
 for(auto &entry:indices){auto &use=command->_shadowUses.at(entry.first);command->_shadows.add(entry.first->contents,use.buffer->_wrapped,entry.second);}command->_shadows.capture(command->_queue->_shadowPool);
 fuxi::ShadowIndexCache cache;uint64_t scanned=0;for(const auto &op:command->_ops)if(op.pipeline)trackShadowDraw(command,op,cache,scanned);
 if(trace){trace->shadowScannedIndices+=scanned;for(auto &entry:command->_shadowUses){auto &use=entry.second;for(size_t i=0;i<trace->shadowFullReasons.size();++i)trace->shadowFullReasons[i]+=use.ranges.fullReason(fuxi::ShadowFullReason(i));
  if(use.textureUses)trace->textureShadowBytes+=use.ranges.bytes();for(size_t i=0;i<trace->textureShadowUses.size();++i)trace->textureShadowUses[i]+=bool(use.textureUses&(1u<<i));}}
}
static uint64_t refreshCommandShadows(FuxiCommandBuffer *command){
 uint64_t bytes=0;for(auto &entry:command->_shadowUses){auto &use=entry.second;bytes+=command->_shadows.refresh(use.buffer->_buffer->contents,use.buffer->_wrapped,use.ranges);}command->_shadows.release(command->_queue->_shadowPool);return bytes;
}
// Scope client methods, including nested convenience adapters. No clock reads
// or atomic updates when this command buffer is not being traced.
#define FRONTEND_TIME(command) fuxi::FrontendTimer frontendTime((command)->_frameTrace && (command)->_status==MTLCommandBufferStatusNotEnqueued?&(command)->_frontendTimes:nullptr)
@implementation FuxiRefusedRender
- (void)rejectSelector:(SEL)s {unsupported(self,s);[_command rejectSelector:s];}
- (void)endEncoding {CENSUS();}
- (void)dealloc {[_command release];[super dealloc];}
@end
@implementation FuxiRender
- (void)rejectSelector:(SEL)s {FRONTEND_TIME(_command);DescriptorContext descriptor(_passText);unsupported(self,s);[_command rejectSelector:s];}
- (void)dealloc {[_command release];[_target release];for(auto t:_targets)[t release];[_pipeline release];for(unsigned i=0;i<fuxi::resourceSlots;++i){[_samples[i] release];[_samplers[i] release];}[super dealloc];}
- (void)active {FRONTEND_TIME(_command);stage4Require("renderEncoder","ended",_ended,!_ended);stage4Require("renderEncoder","encoding",_command->_encoding,_command->_encoding);
 stage4Require("renderEncoder","commandStatus",_command->_status,_command->_status==MTLCommandBufferStatusNotEnqueued);}
- (void)setRenderPipelineState:(id)p {FRONTEND_TIME(_command);FUXI_PHASE(pipeline);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(p && p==_pipeline && object_getClass(p)==[FuxiPipeline class])return;
 if(!p){[_pipeline release];_pipeline=nil;_state.diagnostic.clear();return;}
 if(!stage4Class("renderEncoder","pipeline.class",p,[FuxiPipeline class]))invalid(@"renderEncoder pipeline.class");
 stage4Require("renderEncoder","pipeline.device",(uint64_t)[p device],[p device]==_command->_queue->_device);
 if(p==_pipeline)return;
 {auto pass=_passText;std::shared_ptr<void> hold((void *)[(((FuxiPipeline *)p)->_descriptorText?:@"pipeline=model") retain],[](void *x){[(id)x release];});
  _state.diagnostic=fuxi::LazyText([pass,hold]{return std::string(pass.c_str())+" "+[(NSString *)hold.get() UTF8String];});}
 [p retain];[_pipeline release];_pipeline=p;
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentTexture:(id)t atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(texSamp);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 stage4Require("sampling","textureIndex",i,i<fuxi::resourceSlots);if(t && t==_samples[i] && object_getClass(t)==[FuxiTexture class])return;
 if(!t){[_samples[i] release];_samples[i]=nil;return;}
 if(!stage4Class("sampling","texture.class",t,[FuxiTexture class]))invalid(@"sampling texture.class");
 stage4Require("sampling","texture.device",(uint64_t)[t device],[t device]==_command->_queue->_device);
 stage4Require("sampling","texture.usage",((FuxiTexture *)t)->_usage,!((FuxiTexture *)t)->_usage || (((FuxiTexture *)t)->_usage&MTLTextureUsageShaderRead));
 // Feedback depends on the pipeline, sampler and mip range at draw time.
 if(t!=_samples[i]){[t retain];[_samples[i] release];_samples[i]=t;}
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentSamplerState:(id)s atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(texSamp);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 stage4Require("sampling","samplerIndex",i,i<fuxi::resourceSlots);if(s && s==_samplers[i] && object_getClass(s)==[FuxiSampler class])return;
 if(!s){[_samplers[i] release];_samplers[i]=nil;return;}
 if(!stage4Class("sampling","sampler.class",s,[FuxiSampler class]))invalid(@"sampling sampler.class");
 stage4Require("sampling","sampler.device",(uint64_t)[s device],[s device]==_command->_queue->_device);
 if(s!=_samplers[i]){[s retain];[_samplers[i] release];_samplers[i]=s;}
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)setBuffer:(id)b offset:(NSUInteger)o index:(NSUInteger)i stage:(unsigned)stage bytes:(BOOL)bytes {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);
 [self active];if(i>=fuxi::resourceSlots || (b && (![b isKindOfClass:[FuxiBuffer class]] || [b device]!=_command->_queue->_device || o>=[b length])))invalid(@"buffer slot/device/offset");
 if(b)retainCommandResource(_command,b,true);_state.buffers[stage][i]={b?((FuxiBuffer *)b)->_buffer:nullptr,o,(bool)bytes};
}
- (void)setVertexBuffer:(id)b offset:(NSUInteger)o atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self setBuffer:b offset:o index:i stage:0 bytes:NO];} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentBuffer:(id)b offset:(NSUInteger)o atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self setBuffer:b offset:o index:i stage:1 bytes:NO];} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
// NativeMetal's stage adapters, retaining Fuxi's sixteen-slot/device/poison rules.
- (void)setVertexBufferOffset:(NSUInteger)o atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(i>=fuxi::resourceSlots)invalid(@"vertex buffer offset slot");auto &b=_state.buffers[0][i];
 if(!fuxi::bindingOffset(b.buffer!=nullptr,b.bytes,b.buffer?b.buffer->length:0,o))invalid(@"vertex buffer offset binding/bounds");b.offset=o;
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentBufferOffset:(NSUInteger)o atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(i>=fuxi::resourceSlots)invalid(@"fragment buffer offset slot");auto &b=_state.buffers[1][i];
 if(!fuxi::bindingOffset(b.buffer!=nullptr,b.bytes,b.buffer?b.buffer->length:0,o))invalid(@"fragment buffer offset binding/bounds");b.offset=o;
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setVertexBuffers:(const id<MTLBuffer> __unsafe_unretained [])b offsets:(const NSUInteger [])o withRange:(NSRange)r {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(!fuxi::bindingRange(r.location,r.length) || (r.length && (!b || !o)))invalid(@"vertex buffers range/pointers");
 for(NSUInteger k=0;k<r.length;++k)[self setBuffer:b[k] offset:o[k] index:r.location+k stage:0 bytes:NO];
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentBuffers:(const id<MTLBuffer> __unsafe_unretained [])b offsets:(const NSUInteger [])o withRange:(NSRange)r {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(!fuxi::bindingRange(r.location,r.length) || (r.length && (!b || !o)))invalid(@"fragment buffers range/pointers");
 for(NSUInteger k=0;k<r.length;++k)[self setBuffer:b[k] offset:o[k] index:r.location+k stage:1 bytes:NO];
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentTextures:(const id<MTLTexture> __unsafe_unretained [])t withRange:(NSRange)r {FRONTEND_TIME(_command);FUXI_PHASE(texSamp);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(!fuxi::bindingRange(r.location,r.length) || (r.length && !t))invalid(@"fragment textures range/pointer");
 for(NSUInteger k=0;k<r.length;++k)[self setFragmentTexture:t[k] atIndex:r.location+k];
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentSamplerStates:(const id<MTLSamplerState> __unsafe_unretained [])s withRange:(NSRange)r {FRONTEND_TIME(_command);FUXI_PHASE(texSamp);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(!fuxi::bindingRange(r.location,r.length) || (r.length && !s))invalid(@"fragment samplers range/pointer");
 for(NSUInteger k=0;k<r.length;++k)[self setFragmentSamplerState:s[k] atIndex:r.location+k];
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentTexture:(id<MTLTexture>)t atTextureIndex:(NSUInteger)ti samplerState:(id<MTLSamplerState>)s atSamplerIndex:(NSUInteger)si {FRONTEND_TIME(_command);FUXI_PHASE(texSamp);
 CENSUS();[self setFragmentTexture:t atIndex:ti];[self setFragmentSamplerState:s atIndex:si];
}
- (void)fence:(id)f stages:(MTLRenderStages)stages {FRONTEND_TIME(_command);FUXI_PHASE(state);CENSUS();
 if(_ended || !_command->_encoding || ![f isKindOfClass:[FuxiFence class]] || [f device]!=[self device] || !stages || (stages&~(MTLRenderStageVertex|MTLRenderStageFragment))){[self rejectSelector:_cmd];return;}
 retainCommandResource(_command,f);
}
- (void)updateFence:(id<MTLFence>)f afterStages:(MTLRenderStages)stages {FRONTEND_TIME(_command);FUXI_PHASE(state);[self fence:f stages:stages];}
- (void)waitForFence:(id<MTLFence>)f beforeStages:(MTLRenderStages)stages {FRONTEND_TIME(_command);FUXI_PHASE(state);[self fence:f stages:stages];}
- (void)useResource:(id<MTLResource>)r usage:(MTLResourceUsage)usage stages:(MTLRenderStages)stages {FRONTEND_TIME(_command);FUXI_PHASE(state);CENSUS();(void)usage;(void)stages;
 if(_ended || (r && [r device]!=[self device])){[self rejectSelector:_cmd];return;}if(r)retainCommandResource(_command,r,false,fuxi::ShadowFullReason::useResource);
}
- (void)setDepthStencilState:(id)s {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(s && (![s isKindOfClass:[FuxiDepthStencil class]] || [s device]!=[self device]))invalid(@"depth/stencil state device/class");
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
// CA's update_encoder_state restores these defaults when its dirty bits are
// set. With our sole color target and restricted blend factors they are inert.
// Refuse other raster/query state instead of silently accepting a real effect.
- (void)setFrontFacingWinding:(MTLWinding)w {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 stage4Require("raster","winding",w,w==MTLWindingClockwise);
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setCullMode:(MTLCullMode)m {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 stage4Require("raster","cullMode",m,m==MTLCullModeNone);
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setTriangleFillMode:(MTLTriangleFillMode)m {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 stage4Require("raster","fillMode",m,m==MTLTriangleFillModeFill);
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setDepthBias:(float)bias slopeScale:(float)slope clamp:(float)clamp {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(bias!=0 || (slope!=0 && slope!=1) || clamp!=0)invalid(@"neutral depth bias only (0,0/1,0)");
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setBlendColorRed:(float)red green:(float)green blue:(float)blue alpha:(float)alpha {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 for(float value:{red,green,blue,alpha})if(!std::isfinite(value) || value<0 || value>1)invalid(@"blend color outside 0..1");
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setVisibilityResultMode:(MTLVisibilityResultMode)mode offset:(NSUInteger)offset {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(mode!=MTLVisibilityResultModeDisabled || offset)invalid(@"visibility queries deferred");
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (id)device {FRONTEND_TIME(_command);CENSUS();return _command->_queue->_device;}
- (void)setBytes:(const void *)bytes length:(NSUInteger)n index:(NSUInteger)i stage:(unsigned)stage {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);
 [self active];if(!bytes || !n || n>4096 || i>=fuxi::resourceSlots)invalid(@"inline bytes bound");
 try {
  owner(_command->_queue->_device)->backend->requireLive();
  auto slice=_command->_uploads.copy(bytes,n,_command->_queue->_uploadStorage,[&](size_t capacity){
   FuxiBuffer *b=[FuxiBuffer new];b->_device=[_command->_queue->_device retain];
   try{b->_buffer=owner(b->_device)->backend->allocate(capacity);b->_buffer->upload=true;}catch(...){[b release];throw;}return b;
  },[](FuxiBuffer *b){return b->_buffer->contents;});
  [self setBuffer:slice.handle offset:slice.offset index:i stage:stage bytes:YES];_state.buffers[stage][i].end=slice.offset+slice.length;
 }catch(const std::exception &e){invalid([NSString stringWithUTF8String:e.what()]);}
}
- (void)setVertexBytes:(const void *)bytes length:(NSUInteger)n atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self setBytes:bytes length:n index:i stage:0];} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setFragmentBytes:(const void *)bytes length:(NSUInteger)n atIndex:(NSUInteger)i {FRONTEND_TIME(_command);FUXI_PHASE(bufBytes);@try {CENSUS();DescriptorContext descriptor(_passText);[self setBytes:bytes length:n index:i stage:1];} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setViewport:(MTLViewport)v {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 if(!fuxi::viewportFits(v.originX,v.originY,v.width,v.height,v.znear,v.zfar,_target->_texture->levelWidth(_state.dstLevel),_target->_texture->levelHeight(_state.dstLevel)))invalid(@"viewport bounds");
 _state.viewport={(float)v.originX,(float)(v.originY+v.height),(float)v.width,(float)-v.height,(float)v.znear,(float)v.zfar};
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)setScissorRect:(MTLScissorRect)r {FRONTEND_TIME(_command);FUXI_PHASE(state);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];if(!fuxi::scissorFits(r.x,r.y,r.width,r.height,_state.areaWidth,_state.areaHeight))invalid(@"scissor bounds");
 _state.scissor={{(int32_t)r.x,(int32_t)r.y},{(uint32_t)r.width,(uint32_t)r.height}};
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)record:(MTLPrimitiveType)t start:(NSUInteger)start count:(NSUInteger)count indices:(id)indices offset:(NSUInteger)offset width:(uint32_t)width {FRONTEND_TIME(_command);FUXI_PHASE(draw);fuxi::FrontendDrawTimer drawTime(_command->_frameTrace?&_command->_frontendTimes:nullptr);
 [self active];if(!_pipeline || !_pipeline->_pipeline){fuxi::dropPipelineDraw();return;}
 // Metal ignores an empty draw.
 if(!count)return;
 if((t!=MTLPrimitiveTypeTriangle && t!=MTLPrimitiveTypeTriangleStrip && t!=MTLPrimitiveTypeLine && t!=MTLPrimitiveTypeLineStrip) || count>65536 || start>65536-count)
  invalid([NSString stringWithFormat:@"draw bounds/topology type=%lu start=%lu count=%lu indexed=%d",(unsigned long)t,(unsigned long)start,(unsigned long)count,indices!=nil]);
 if(t==MTLPrimitiveTypeLine)count&=~NSUInteger(1);
 if(!count || (t==MTLPrimitiveTypeLineStrip && count<2))return;
 if(indices && (![indices isKindOfClass:[FuxiBuffer class]] || [indices device]!=_command->_queue->_device || !fuxi::shadowIndexBytes([indices length],offset,(uint32_t)count,width)))invalid(@"index bounds/device");
 DescriptorContext context(_passText);
 for(unsigned slot=0;slot<fuxi::resourceSlots;++slot)if(_samples[slot] && _samples[slot]->_surface){
  for(unsigned k=0;k<_state.colorCount;++k){auto target=k?_targets[k-1]:((_state.colorMask&1)?_target:nil);
   if(target && target->_surface && IOSurfaceGetID(_samples[slot]->_surface)==IOSurfaceGetID(target->_surface)){
    if(fuxi::skipSamplingDraw("IOSurface attachment feedback"))return;
    stage4Require("sampling","attachmentFeedback",k,false);
   }
  }
 }
 _command->_ops.push_back(_state);auto &op=_command->_ops.back();op.renderDraw=true;op.texture=_target->_texture;op.pipeline=_pipeline->_pipeline;op.vertices=(uint32_t)count;op.first=(uint32_t)start;op.topology=t==MTLPrimitiveTypeLine?fuxi::Topology::Line:t==MTLPrimitiveTypeLineStrip?fuxi::Topology::LineStrip:t==MTLPrimitiveTypeTriangleStrip?fuxi::Topology::TriangleStrip:fuxi::Topology::Triangle;
 op.indices=indices?((FuxiBuffer *)indices)->_buffer:nullptr;op.indexOffset=offset;op.indexWidth=width;
 // CA smoke diagnostic: for a textured draw, every vertex binding and its first vertices (stride 48 floats).
 bool textured=false;for(size_t i=0;i<fuxi::resourceSlots;++i)textured|=_samples[i]!=nil;
 static std::atomic<unsigned> texturedTraces{0};
 if(caTrace() && textured && texturedTraces.fetch_add(1)<8) {
  fprintf(stderr,"FUXI MTL: trace textured draw start %lu count %lu indexed %d\n",(unsigned long)start,(unsigned long)count,indices!=nil);
  for(size_t i=0;i<fuxi::resourceSlots;++i)if(_samples[i])fprintf(stderr,"FUXI MTL: trace textured slot %zu tex %lux%lu\n",i,(unsigned long)[_samples[i] width],(unsigned long)[_samples[i] height]);
  for(size_t i=0;i<fuxi::resourceSlots;++i) {const fuxi::BufferBind &v=_state.buffers[0][i];if(!v.buffer)continue;
   fprintf(stderr,"FUXI MTL: trace textured vbuf %zu offset %llu length %llu bytes %d\n",i,(unsigned long long)v.offset,(unsigned long long)v.buffer->length,v.bytes);
   for(unsigned k=0;k<4;++k) {uint64_t at=v.offset+k*48;if(at+32>v.buffer->length)break;const float *f=(const float *)((const uint8_t *)v.buffer->contents+at);
    fprintf(stderr,"FUXI MTL: trace textured vbuf %zu v%u %.3f %.3f %.3f %.3f | %.4f %.4f | %.4f %.4f\n",i,k,f[0],f[1],f[2],f[3],f[4],f[5],f[6],f[7]);}
  }
  for(unsigned stage=0;stage<2;++stage)for(size_t i=0;i<fuxi::resourceSlots;++i) {const fuxi::BufferBind &v=_state.buffers[stage][i];
   if(!v.buffer || v.offset+64>v.buffer->length)continue;const float *f=(const float *)((const uint8_t *)v.buffer->contents+v.offset);
   fprintf(stderr,"FUXI MTL: trace textured %s buf %zu raw",stage?"frag":"vert",i);for(unsigned k=0;k<16;++k)fprintf(stderr," %.4g",f[k]);fputc('\n',stderr);
  }
 }
 op.load=_drawn?1:_load;op.store=1;
 for(unsigned i=1;i<op.colorCount;++i){op.colors[i-1].load=_drawn?1:_state.colors[i-1].load;op.colors[i-1].store=1;if(_targets[i-1])retainCommandTexture(_command,_targets[i-1],false);}
 for(unsigned i=0;i<fuxi::resourceSlots;++i){op.samples[i]=_samples[i]?_samples[i]->_texture:nullptr;if(_samples[i]){op.sampleBase[i]=_samples[i]->_baseLevel;op.sampleCount[i]=(uint32_t)[_samples[i] mipmapLevelCount];}op.samplers[i]=_samplers[i]?_samplers[i]->_sampler:nullptr;
 if(_samples[i])retainCommandTexture(_command,_samples[i],true);if(_samplers[i])retainCommandResource(_command,_samplers[i]);}
 if(indices){retainCommandResource(_command,indices,true);auto it=_command->_shadowUses.find(op.indices);if(it!=_command->_shadowUses.end())it->second.ranges.add(offset,uint64_t(count)*width);}
 op.clear[0]=_clear.red;op.clear[1]=_clear.green;op.clear[2]=_clear.blue;op.clear[3]=_clear.alpha;
 retainCommandTexture(_command,_target,false);retainCommandResource(_command,_pipeline);_lastOp=_command->_ops.size()-1;_drawn=YES;
}
- (void)drawPrimitives:(MTLPrimitiveType)t vertexStart:(NSUInteger)start vertexCount:(NSUInteger)count {FRONTEND_TIME(_command);FUXI_PHASE(draw);@try {CENSUS();DescriptorContext descriptor(_passText);[self record:t start:start count:count indices:nil offset:0 width:2];} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)drawPrimitives:(MTLPrimitiveType)t vertexStart:(NSUInteger)start vertexCount:(NSUInteger)count instanceCount:(NSUInteger)instances {FRONTEND_TIME(_command);FUXI_PHASE(draw);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 stage4Require("draw","instanceCount",instances,instances==1);[self record:t start:start count:count indices:nil offset:0 width:2];
} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)drawIndexedPrimitives:(MTLPrimitiveType)t indexCount:(NSUInteger)count indexType:(MTLIndexType)type indexBuffer:(id)b indexBufferOffset:(NSUInteger)offset {FRONTEND_TIME(_command);FUXI_PHASE(draw);@try {CENSUS();DescriptorContext descriptor(_passText);// UInt32 stays refused: the draw-plan rebase bounds assume 2-byte indices.
 if(type!=MTLIndexTypeUInt16 || !b)invalid(@"index type/buffer");[self record:t start:0 count:count indices:b offset:offset width:type==MTLIndexTypeUInt16?2:4];} @catch(NSException *e){(void)e;[self rejectSelector:_cmd];}}
- (void)endEncoding {FRONTEND_TIME(_command);FUXI_PHASE(encEnd);@try {CENSUS();DescriptorContext descriptor(_passText);[self active];
 DescriptorContext context(_passText);
 if(!_drawn){fuxi::Operation op=_state;op.diagnostic=_passText;op.texture=_target->_texture;op.clearOnly=true;op.load=_load;op.store=_store;
 op.clear[0]=_clear.red;op.clear[1]=_clear.green;op.clear[2]=_clear.blue;op.clear[3]=_clear.alpha;
 retainCommandTexture(_command,_target,false);for(unsigned i=1;i<op.colorCount;++i)if(_targets[i-1])retainCommandTexture(_command,_targets[i-1],false);_command->_ops.push_back(op);
 }else {auto &op=_command->_ops[_lastOp];op.store=_store;for(unsigned i=1;i<op.colorCount;++i)op.colors[i-1].store=_state.colors[i-1].store;}
 _ended=YES;_command->_encoding=NO;} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}


@end
// The descriptor is copied at encoder creation; its text is formatted lazily for failure logs.
static NSString *passAttachmentText(MTLRenderPassAttachmentDescriptor *a){
 id t=a.texture;NSString *texture=@"nil";
 if([t isKindOfClass:[FuxiTexture class]]){auto ft=(FuxiTexture *)t;texture=[NSString stringWithFormat:@"{%@ viewBase=%u viewLevels=%lu}",ft->_descriptorText?:@"model",ft->_baseLevel,(unsigned long)[ft mipmapLevelCount]];}
 else if(t)texture=[NSString stringWithFormat:@"class=%s",object_getClassName(t)];
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
 return [NSString stringWithFormat:@"texture=%@ level=%lu slice=%lu plane=%lu load=%lu store=%lu options=%lu resolve=%s/%lu/%lu/%lu",texture,(unsigned long)a.level,(unsigned long)a.slice,(unsigned long)a.depthPlane,(unsigned long)a.loadAction,(unsigned long)a.storeAction,(unsigned long)a.storeActionOptions,a.resolveTexture?object_getClassName(a.resolveTexture):"nil",(unsigned long)a.resolveLevel,(unsigned long)a.resolveSlice,(unsigned long)a.resolveDepthPlane];
#pragma clang diagnostic pop
}
static NSString *renderPassText(MTLRenderPassDescriptor *d){
 NSMutableString *text=[NSMutableString stringWithFormat:@"renderPass area=%lux%lu array=%lu samples=%lu visibility=%u tile=%lux%lu imageblock=%lu threadgroup=%lu rateMap=%u colors=[",(unsigned long)d.renderTargetWidth,(unsigned long)d.renderTargetHeight,(unsigned long)d.renderTargetArrayLength,(unsigned long)d.defaultRasterSampleCount,d.visibilityResultBuffer!=nil,(unsigned long)d.tileWidth,(unsigned long)d.tileHeight,(unsigned long)d.imageblockSampleLength,(unsigned long)d.threadgroupMemoryLength,d.rasterizationRateMap!=nil];
 for(unsigned i=0;i<8;++i){auto a=d.colorAttachments[i];auto c=a.clearColor;[text appendFormat:@"%u:%@ clear=%.17g,%.17g,%.17g,%.17g;",i,passAttachmentText(a),c.red,c.green,c.blue,c.alpha];}
 [text appendFormat:@"] depth={%@ clear=%.17g} stencil={%@ clear=%u}",passAttachmentText(d.depthAttachment),d.depthAttachment.clearDepth,passAttachmentText(d.stencilAttachment),d.stencilAttachment.clearStencil];return text;
}
@implementation FuxiQueue
- (void)dealloc {_uploadStorage.clear([](FuxiBuffer *b){[b release];});for(auto q:{_completionQueue,_submitQueue,_retireQueue,_finishQueue,_deliveryQueue})if(q)dispatch_release(q);if(_inflight)dispatch_release(_inflight);[_device release];[super dealloc];}
- (id)device {CENSUS();return _device;}
- (id)commandBuffer {CENSUS();FUXI_PHASE(cbMisc);
 FuxiCommandBuffer *b=[FuxiCommandBuffer new];b->_queue=[self retain];b->_ops=_operationStorage.acquire();
 if(fuxi::systemTranslationPolicy().frameTrace && !strcmp(clientProcess,"backboardd")){
  b->_frameTrace=true;b->_traceCreated=fuxi::FrameClock::now();b->_traceCounts=fuxi::frameCounts;b->_traceThread=std::this_thread::get_id();
 }
 b->_resources=[NSMutableArray new];b->_handlers=[NSMutableArray new];b->_scheduledHandlers=[NSMutableArray new];b->_completionGroup=dispatch_group_create();b->_overlap=fuxi::systemTranslationPolicy().overlap;b->_earlySubmit=fuxi::earlyScheduledWait(fuxi::systemTranslationPolicy(),gpuScanout,gpuScanoutQuiet,clientProcess);if(b->_overlap){b->_scheduledGroup=dispatch_group_create();b->_commitReady=dispatch_group_create();}return [b autorelease];
}
// NativeMetal retains owners even for the unretained entry point. Resources
// remain alive through synchronous completion; this only strengthens lifetime.
- (id)commandBufferWithUnretainedReferences {CENSUS();return [self commandBuffer];}
- (id)commandBufferWithDescriptor:(MTLCommandBufferDescriptor *)d {CENSUS();
 if(!d || d.errorOptions!=MTLCommandBufferErrorOptionNone){unsupported(self,_cmd);return nil;}return [self commandBuffer];
}
- (void)setSubmissionQueue:(dispatch_queue_t)queue {CENSUS();(void)queue;}
- (void)setCompletionQueue:(dispatch_queue_t)queue {CENSUS();if(queue)dispatch_retain(queue);if(_completionQueue)dispatch_release(_completionQueue);_completionQueue=queue;if(_deliveryQueue)dispatch_set_target_queue(_deliveryQueue,queue?:dispatch_get_global_queue(compositorQoS(),0));}
- (int)setOwnerWithIdentity:(unsigned)identity {CENSUS();(void)identity;return 46;}
- (void)setFineGrainedBackgroundVisibilityEnabled:(BOOL)enabled {CENSUS();(void)enabled;}
- (BOOL)setGPUPriority:(NSUInteger)priority {CENSUS();(void)priority;return NO;}
- (BOOL)setBackgroundGPUPriority:(NSUInteger)priority {CENSUS();(void)priority;return NO;}
- (NSUInteger)getGPUPriority {CENSUS();return 1;}
- (NSUInteger)getBackgroundGPUPriority {CENSUS();return 3;}
@end
@implementation FuxiCommandBuffer
- (void)rejectSelector:(SEL)s {FRONTEND_TIME(self);
 unsupported(self,s);_rejected=YES;
 if(!_error)_error=[[NSError alloc] initWithDomain:MTLCommandBufferErrorDomain code:MTLCommandBufferErrorInvalidResource
  userInfo:@{NSLocalizedDescriptionKey:[NSString stringWithFormat:@"unsupported %@",NSStringFromSelector(s)]}];
}
- (void)dealloc {if(_queue){_shadows.release(_queue->_shadowPool);_queue->_operationStorage.release(_ops);_uploads.release(_queue->_uploadStorage,owner(_queue->_device)->backend && !owner(_queue->_device)->backend->poisoned,[](FuxiBuffer *b){[b release];});}[_queue release];[_resources release];[_handlers release];[_scheduledHandlers release];if(_completionGroup)dispatch_release(_completionGroup);if(_scheduledGroup)dispatch_release(_scheduledGroup);if(_commitReady)dispatch_release(_commitReady);[_error release];[super dealloc];}
- (id)device {FRONTEND_TIME(self);CENSUS();return _queue->_device;}
- (id)commandQueue {FRONTEND_TIME(self);CENSUS();return _queue;}
- (MTLCommandBufferStatus)status {CENSUS();if(!_overlap)return _status;@synchronized(self){return _overlap && _enqueued && _status==MTLCommandBufferStatusNotEnqueued?MTLCommandBufferStatusEnqueued:_status;}}
- (NSError *)error {CENSUS();if(!_overlap)return _error;@synchronized(self){return [[_error retain] autorelease];}}
- (BOOL)retainedReferences {FRONTEND_TIME(self);CENSUS();return YES;}
- (id)blitCommandEncoder {FRONTEND_TIME(self);FUXI_PHASE(blit);CENSUS();
 if(_rejected || _status!=MTLCommandBufferStatusNotEnqueued || _encoding) {unsupported(self,_cmd);return nil;}
 FuxiBlit *e=[FuxiBlit new];e->_command=[self retain];_encoding=YES;return [e autorelease];
}
- (id)refusedRender {if(!_rejected)[self rejectSelector:@selector(renderCommandEncoderWithDescriptor:)];FuxiRefusedRender *e=[FuxiRefusedRender new];e->_command=[self retain];return [e autorelease];}
- (id)renderCommandEncoderWithDescriptor:(MTLRenderPassDescriptor *)d {FRONTEND_TIME(self);FUXI_PHASE(encBegin);@try {CENSUS();
 if(clientMode==fuxi::ClientMode::census)return nil;if(_rejected)return [self refusedRender];
 auto checksPhase=std::make_unique<fuxi::PhaseTimer>(fuxi::EncPhase::encChecks);
 std::shared_ptr<void> passCopy((void *)[d copy],[](void *p){[(id)p release];});
 fuxi::LazyText passText([passCopy]{return std::string(renderPassText((MTLRenderPassDescriptor *)passCopy.get()).UTF8String);});DescriptorContext context(passText);
 if(!stage4Value("renderEncoder","commandStatus",_status,_status==MTLCommandBufferStatusNotEnqueued) ||
    !stage4Value("renderEncoder","encoding",_encoding,!_encoding))return [self refusedRender];
#define CHECK(field,value,accepted) do {__typeof__(value) actual=(value);stage4Require("renderPass",field,(uint64_t)actual,(accepted));}while(0)
 CHECK("descriptor",d,actual!=nil);
 unsigned first=8;uint32_t colorCount=1,colorMask=0;
 for(unsigned i=0;i<8;++i)if(d.colorAttachments[i].texture){if(first==8)first=i;colorCount=i+1;colorMask|=1u<<i;}
 for(unsigned i=0;i<8;++i)if(!d.colorAttachments[i].texture && d.colorAttachments[i].resolveTexture){char field[80];snprintf(field,sizeof(field),"colorAttachments[%u].resolveTexture",i);CHECK(field,d.colorAttachments[i].resolveTexture,!actual);}
 CHECK("colorAttachments.empty",colorMask,actual);
 auto a=d.colorAttachments[first];id t=a.texture;
 if(!stage4Class("renderPass","primaryTexture.class",t,[FuxiTexture class]))invalid(@"renderPass texture.class");
 for(unsigned i=0;i<colorCount;++i){auto c=d.colorAttachments[i];id texture=c.texture;char field[96];if(!texture){CHECK("absent.resolveTexture",c.resolveTexture,!actual);continue;}
#define COLOR_CHECK(name,value,accepted) do{__typeof__(value) actual=(value);if(!(accepted)){snprintf(field,sizeof(field),"colorAttachments[%u]." name,i);stage4Require("renderPass",field,(uint64_t)actual,false);}}while(0)
  snprintf(field,sizeof(field),"colorAttachments[%u].texture.class",i);if(!stage4Class("renderPass",field,texture,[FuxiTexture class]))invalid(@"renderPass texture.class");
  COLOR_CHECK("device",[texture device],actual==[self device]);
  COLOR_CHECK("usage",((FuxiTexture *)texture)->_usage,!actual || (actual&MTLTextureUsageRenderTarget));
  COLOR_CHECK("level",c.level,actual<[texture mipmapLevelCount]);COLOR_CHECK("slice",c.slice,!actual);COLOR_CHECK("depthPlane",c.depthPlane,!actual);
  COLOR_CHECK("resolveTexture",c.resolveTexture,!actual);COLOR_CHECK("resolveLevel",c.resolveLevel,!actual);COLOR_CHECK("resolveSlice",c.resolveSlice,!actual);COLOR_CHECK("resolveDepthPlane",c.resolveDepthPlane,!actual);
  COLOR_CHECK("loadAction",c.loadAction,actual<=MTLLoadActionClear);COLOR_CHECK("storeAction",c.storeAction,actual==MTLStoreActionDontCare || actual==MTLStoreActionStore);
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
  COLOR_CHECK("storeActionOptions",c.storeActionOptions,!actual);
#pragma clang diagnostic pop
  auto clear=c.clearColor;for(double x:{clear.red,clear.green,clear.blue,clear.alpha})if(!std::isfinite(x))invalid(@"nonfinite clearColor");
  COLOR_CHECK("widthAtLevel",std::max(NSUInteger(1),[texture width]>>c.level),actual==std::max(NSUInteger(1),[t width]>>a.level));
  COLOR_CHECK("heightAtLevel",std::max(NSUInteger(1),[texture height]>>c.level),actual==std::max(NSUInteger(1),[t height]>>a.level));
#undef COLOR_CHECK
 }
 // Attachment objects are lazily created on iOS too: gate their textures.
 CHECK("depthAttachment.texture",d.depthAttachment.texture,!actual);CHECK("stencilAttachment.texture",d.stencilAttachment.texture,!actual);
 CHECK("depthAttachment.resolveTexture",d.depthAttachment.resolveTexture,!actual);CHECK("stencilAttachment.resolveTexture",d.stencilAttachment.resolveTexture,!actual);
 CHECK("visibilityResultBuffer",d.visibilityResultBuffer,!actual);
 CHECK("renderTargetArrayLength",d.renderTargetArrayLength,actual<=1);
 CHECK("imageblockSampleLength",d.imageblockSampleLength,!actual);CHECK("tileWidth",d.tileWidth,!actual);CHECK("tileHeight",d.tileHeight,!actual);
 CHECK("threadgroupMemoryLength",d.threadgroupMemoryLength,!actual);CHECK("defaultRasterSampleCount",d.defaultRasterSampleCount,actual<=1);
 CHECK("rasterizationRateMap",d.rasterizationRateMap,!actual);
 uint32_t targetW=(uint32_t)std::max(NSUInteger(1),[t width]>>a.level),targetH=(uint32_t)std::max(NSUInteger(1),[t height]>>a.level);
 // A nonzero renderTargetWidth/Height restricts the pass to a top-left area of the attachment.
 CHECK("renderTargetWidth",d.renderTargetWidth,actual<=targetW);CHECK("renderTargetHeight",d.renderTargetHeight,actual<=targetH);
 uint32_t areaW=(uint32_t)(d.renderTargetWidth?d.renderTargetWidth:targetW),areaH=(uint32_t)(d.renderTargetHeight?d.renderTargetHeight:targetH);
 MTLSamplePosition ignoredPosition{};CHECK("samplePositions.count",[d getSamplePositions:&ignoredPosition count:0],!actual);

#undef CHECK
 checksPhase.reset();
 FuxiRender *e=[FuxiRender new];e->_state={};e->_state.encoderID=++_renderEncoderID;e->_state.colorCount=colorCount;e->_state.colorMask=colorMask;e->_passText=passText;e->_command=[self retain];e->_target=[t retain];e->_clear=a.clearColor;e->_load=(uint32_t)a.loadAction;e->_store=(uint32_t)a.storeAction;
 e->_state.viewport={0,(float)areaH,(float)areaW,-(float)areaH,0,1};e->_state.scissor={{0,0},{areaW,areaH}};e->_state.dstLevel=((FuxiTexture *)t)->_baseLevel+(uint32_t)a.level;e->_state.areaWidth=areaW;e->_state.areaHeight=areaH;
 for(unsigned i=1;i<colorCount;++i){auto c=d.colorAttachments[i];auto texture=(FuxiTexture *)c.texture;if(!texture)continue;e->_targets[i-1]=[texture retain];auto clear=c.clearColor;
  e->_state.colors[i-1]={texture->_texture,texture->_baseLevel+(uint32_t)c.level,(uint32_t)c.loadAction,(uint32_t)c.storeAction,{(float)clear.red,(float)clear.green,(float)clear.blue,(float)clear.alpha}};}
 _encoding=YES;return [e autorelease];
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];return [self refusedRender];}}
- (void)addScheduledHandler:(MTLCommandBufferHandler)handler {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);CENSUS();
 if(!handler){[self rejectSelector:_cmd];return;}
 @synchronized(self){if(!_scheduled){if(_status<MTLCommandBufferStatusCompleted){id copied=[handler copy];[_scheduledHandlers addObject:copied];[copied release];}return;}}
 handler((id)self);
}
- (void)publishScheduled {
 NSArray *handlers=nil;
 @synchronized(self){if(!_scheduled)return;handlers=[_scheduledHandlers copy];[_scheduledHandlers removeAllObjects];}
 for(MTLCommandBufferHandler handler in handlers)handler((id)self);[handlers release];
}
- (void)deliverCompletedHandlers:(NSArray *)handlers {
 if(!handlers.count)return;dispatch_group_enter(_completionGroup);
 auto queued=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
 void (^run)(void)=^{auto start=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
  if(_frameTrace)_queue->_traceDelivery=std::max<uint64_t>(1,fuxi::frameNanos(queued));
  FuxiCommandBuffer *before=completionCommand;completionCommand=self;
  for(MTLCommandBufferHandler handler in handlers)handler((id)self);
  completionCommand=before;if(_frameTrace)_queue->_traceHandlers=std::max<uint64_t>(1,fuxi::frameNanos(start));dispatch_group_leave(_completionGroup);};
 if(_overlap)dispatch_async(_queue->_deliveryQueue,run);
 else if(_queue->_completionQueue)dispatch_async(_queue->_completionQueue,run);else run();
}
- (void)addCompletedHandler:(MTLCommandBufferHandler)handler {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);CENSUS();
 if(!handler){[self rejectSelector:_cmd];return;}
 @synchronized(self){if(_finished || (!_overlap && _status>=MTLCommandBufferStatusCompleted)){id copied=[handler copy];[self deliverCompletedHandlers:@[copied]];[copied release];return;}
  id copied=[handler copy];[_handlers addObject:copied];[copied release];}
}
- (void)presentDrawable:(id<MTLDrawable>)drawable {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);CENSUS();if(!drawable){[self rejectSelector:_cmd];return;}
 BOOL onlyAfterSuccess=_overlap;[self addCompletedHandler:^(id<MTLCommandBuffer> buffer){if(!onlyAfterSuccess || buffer.status==MTLCommandBufferStatusCompleted)[drawable present];}];}
- (void)presentDrawable:(id<MTLDrawable>)drawable atTime:(CFTimeInterval)time {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);CENSUS();if(!drawable){[self rejectSelector:_cmd];return;}
 BOOL onlyAfterSuccess=_overlap;[self addCompletedHandler:^(id<MTLCommandBuffer> buffer){if(!onlyAfterSuccess || buffer.status==MTLCommandBufferStatusCompleted)[drawable presentAtTime:time];}];}
- (void)presentDrawable:(id<MTLDrawable>)drawable afterMinimumDuration:(CFTimeInterval)duration {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);CENSUS();if(!drawable){[self rejectSelector:_cmd];return;}
 BOOL onlyAfterSuccess=_overlap;[self addCompletedHandler:^(id<MTLCommandBuffer> buffer){if(!onlyAfterSuccess || buffer.status==MTLCommandBufferStatusCompleted)[drawable presentAfterMinimumDuration:duration];}];}
- (void)event:(id)e value:(uint64_t)value signal:(BOOL)signal {FRONTEND_TIME(self);CENSUS();
 if(_status!=MTLCommandBufferStatusNotEnqueued || _encoding || _events.size()>=256 || ![e isKindOfClass:[FuxiEvent class]] || [e device]!=[self device]){[self rejectSelector:_cmd];return;}
 [_resources addObject:e];_events.push_back({_ops.size(),e,value,(bool)signal});
}
- (void)encodeSignalEvent:(id<MTLEvent>)e value:(uint64_t)value {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);[self event:e value:value signal:YES];}
- (void)encodeWaitForEvent:(id<MTLEvent>)e value:(uint64_t)value {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);[self event:e value:value signal:NO];}
- (void)commitSynchronous {
 fuxi::FrontendTimer frontendTime(_frameTrace?&_frontendTimes:nullptr);
 if(_status!=MTLCommandBufferStatusNotEnqueued)return;
 if(_encoding)[self rejectSelector:@selector(commit)];
 _status=MTLCommandBufferStatusCommitted;
 bool backboard=!strcmp(clientProcess,"backboardd");
 auto frameStart=std::chrono::steady_clock::now();uint64_t gpuBefore=backboard?fuxi::Backend::gpuNanos.load():0;
 fuxi::FrameSample trace;bool surfaceFrame=false;
 auto countsBefore=fuxi::frameCounts;
 if(_frameTrace){trace.cpuBeforeCommit=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(frameStart-_traceCreated).count());trace.createdNs=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(_traceCreated.time_since_epoch()).count());trace.cpu=fuxi::frameCPU();
  trace.encodeCountsUnknown=_traceThread!=std::this_thread::get_id();if(!trace.encodeCountsUnknown)countsBefore=_traceCounts;
 }
 {fuxi::FrameTraceScope traceScope(_frameTrace?&trace:nullptr);
 // Deliberately synchronous M4 queue: no dispatch worker or asynchronous promise.
 if(backboardSurvey.active() && fuxi::backboardGate.blocked()){
  for(const auto &point:_events)if(point.signal)[point.event setSignaledValue:point.value];
  _scheduled=YES;_status=MTLCommandBufferStatusCompleted;[_error release];_error=nil;finishSurvey();
 }else try {if(fuxi::backboardGate.blocked())throw std::runtime_error("backboardd failure latched: CPU on next respawn");if(!strcmp(clientProcess,"fuxi-ca-smoke") && failureCount.load())throw std::runtime_error("CA diagnostic failure latched: GPU submission refused");if(_rejected)throw std::runtime_error("unsupported command: GPU submission refused");if(clientMode==fuxi::ClientMode::census)throw std::runtime_error("census: no GPU submission");auto *backend=owner(_queue->_device)->backend;size_t begin=0;
 if(gpuScanout)backend->requireLive();
 auto leaseStart=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
 fuxi::SurfaceLease lease(gpuScanout,lockScanoutSurface,unlockScanoutSurface,&backend->poisoned);
 if(gpuScanout)for(auto &entry:commandSurfaces(self))lease.add(entry.first,entry.second);
 if(_frameTrace)fuxi::addLeaseTrace(trace,lease,fuxi::frameNanos(leaseStart));
 auto shadowStart=std::chrono::steady_clock::now();
 prepareCommandShadows(self,_frameTrace?&trace:nullptr);if(_frameTrace)trace.snapshotBytes+=_shadows.bytes();uint64_t shadowBytes=refreshCommandShadows(self);if(_frameTrace)trace.shadowCopyBytes+=shadowBytes;
 auto shadowNanos=fuxi::frameNanos(shadowStart);fuxi::Backend::shadowCopyNanos+=shadowNanos;if(_frameTrace)trace.shadowCopy+=shadowNanos;
 frontendTime.stop();if(_frameTrace){_frontendTimes.copyTo(trace);fuxi::drainEncodePhases(trace);}
 for(const auto &point:_events){
  if(point.operation>begin){backend->submit(std::vector<fuxi::Operation>(_ops.begin()+begin,_ops.begin()+point.operation),[&]{_scheduled=YES;});begin=point.operation;}
  if(point.signal)[point.event setSignaledValue:point.value];
  else if(![point.event waitUntilSignaledValue:point.value timeoutMS:2000])throw std::runtime_error("process-local event wait timed out");
 }
 if(_events.empty())backend->submit(_ops,[&]{_scheduled=YES;});
 else if(begin<_ops.size())backend->submit(std::vector<fuxi::Operation>(_ops.begin()+begin,_ops.end()),[&]{_scheduled=YES;});
 else _scheduled=YES;
 for(const auto &op:_ops)if(op.pipeline)completedDraws.fetch_add(1);
 if(backboard)for(id r in _resources) {
  if(![r isKindOfClass:[FuxiTexture class]] || ![(FuxiTexture *)r iosurface])continue;
  auto *texture=((FuxiTexture *)r)->_texture;if(!texture)continue;
  for(const auto &op:_ops)if(op.pipeline || op.clearOnly)for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);if(c.texture==texture && c.store && texture->isDefined(c.level)){surfaceFrame=true;break;}}
  if(surfaceFrame)break;
 }
 _status=MTLCommandBufferStatusCompleted;
 if(surfaceFrame && !_frameTrace)fuxi::backboardGate.completed(fuxi::Backend::gpuNanos.load()-gpuBefore,
  std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-frameStart).count());
 }
 catch(const std::exception &e) {
  failureLog("FUXI MTL: submit FAIL %s\n",e.what());_status=MTLCommandBufferStatusError;
  if(!_error)_error=[[NSError alloc] initWithDomain:MTLCommandBufferErrorDomain code:MTLCommandBufferErrorInternal
      userInfo:@{NSLocalizedDescriptionKey:[NSString stringWithUTF8String:e.what()]}];
 }
 if(backboardSurvey.active() && fuxi::backboardGate.blocked()){
  _scheduled=YES;_status=MTLCommandBufferStatusCompleted;[_error release];_error=nil;
  for(const auto &point:_events)if(point.signal)[point.event setSignaledValue:point.value];
 }
 if(_frameTrace && surfaceFrame && _status==MTLCommandBufferStatusCompleted){
  trace.wall=fuxi::frameNanos(frameStart);
  trace.counts={fuxi::frameCounts.pipelines-countsBefore.pipelines,fuxi::frameCounts.descriptors-countsBefore.descriptors,fuxi::frameCounts.allocations-countsBefore.allocations};
  fuxi::backboardGate.completed(fuxi::Backend::gpuNanos.load()-gpuBefore,trace.wall/1e6,&trace);
 }
 } // Restore trace attribution before user callbacks can submit another command.
 // Deliver outside Backend's mutex; handlers may create resources/commands.
 // On successful submission this callback may arrive after GPU completion.
 if(_scheduled){NSArray *scheduled=[_scheduledHandlers copy];[_scheduledHandlers removeAllObjects];
  for(MTLCommandBufferHandler handler in scheduled)handler((id)self);[scheduled release];}
 NSArray *handlers=[_handlers copy];[_handlers removeAllObjects];
 [self deliverCompletedHandlers:handlers];
 [handlers release];[_scheduledHandlers removeAllObjects];[_resources removeAllObjects];_resourceCache.clear();_resourceSet.clear();_sampledRes.clear();_otherRes.clear();_queue->_operationStorage.release(_ops);_uploads.release(_queue->_uploadStorage,owner(_queue->_device)->backend && !owner(_queue->_device)->backend->poisoned,[](FuxiBuffer *b){[b release];});_events.clear();_shadowUses.clear();
}
- (void)commit {CENSUS();
 if(!_overlap){[self commitSynchronous];return;}
 fuxi::FrontendTimer frontendTime(_frameTrace?&_frontendTimes:nullptr);
 if(_status!=MTLCommandBufferStatusNotEnqueued)return;
 if(_encoding)[self rejectSelector:_cmd];
 _commitStart=fuxi::FrameClock::now();
 if(_frameTrace){
  auto previous=_queue->_traceReturn.load();auto now=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(_commitStart.time_since_epoch()).count());
  if(previous && now>previous && now-previous<100000000)_flightTrace.commitGap=now-previous;
  _flightTrace.schedWait=_queue->_traceWaitScheduled.load();_flightTrace.priorDelivery=_queue->_traceDelivery.load();_flightTrace.priorHandlers=_queue->_traceHandlers.load();_flightTrace.priorWaitCompleted=_queue->_traceWaitCompleted.load();_flightTrace.cpuBeforeCommit=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(_commitStart-_traceCreated).count());_flightTrace.createdNs=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(_traceCreated.time_since_epoch()).count());_flightTrace.cpu=fuxi::frameCPU();
  _flightTrace.encodeCountsUnknown=_traceThread!=std::this_thread::get_id();
  if(!_flightTrace.encodeCountsUnknown)_flightTrace.counts={fuxi::frameCounts.pipelines-_traceCounts.pipelines,fuxi::frameCounts.descriptors-_traceCounts.descriptors,fuxi::frameCounts.allocations-_traceCounts.allocations};
 }
 @synchronized(self){_status=MTLCommandBufferStatusCommitted;}
 dispatch_group_enter(_completionGroup);dispatch_group_enter(_scheduledGroup);
 // Reserve queue order before snapshotting; explicit enqueue reserves it earlier.
 if(!_enqueued)[self reserveOverlap];
 // Snapshot caller-owned no-copy bytes before commit returns. The worker drains
 // prior submissions before refreshing a shared shadow allocation.
 auto snapshotStart=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
 try{if(_earlySubmit){auto *backend=owner(_queue->_device)->backend;backend->requireLive();
   _pendingLease=std::make_shared<fuxi::SurfaceLease>(true,lockScanoutPendingSurface,unlockScanoutSurface,&backend->poisoned);
   for(auto &entry:commandSurfaces(self))if(entry.second)_pendingLease->add(entry.first); // Sampled-only surfaces need no producer reservation.
  }
  prepareCommandShadows(self,_frameTrace?&_flightTrace:nullptr);for(auto &entry:_shadowUses){auto &use=entry.second;_shadows.add(use.buffer->_buffer->shadowKey(),use.buffer->_wrapped,use.ranges);}
  _shadows.capture(_queue->_shadowPool);if(_frameTrace)_flightTrace.snapshotBytes+=_shadows.bytes();
 }catch(const std::exception &e){[self submissionError:e.what()];}
 _commitReturn=fuxi::FrameClock::now();
 if(_frameTrace){_flightTrace.snapshot=fuxi::frameNanos(snapshotStart);_flightTrace.commitCall=fuxi::frameNanos(_commitStart);_queue->_traceReturn=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(_commitReturn.time_since_epoch()).count());}
 frontendTime.stop();if(_frameTrace){_frontendTimes.copyTo(_flightTrace);fuxi::drainEncodePhases(_flightTrace);}
 dispatch_group_leave(_commitReady);
}
- (void)submissionError:(const char *)message {
 failureLog("FUXI MTL: submit FAIL %s\n",message);
 @synchronized(self){_status=MTLCommandBufferStatusError;if(!_error)_error=[[NSError alloc] initWithDomain:MTLCommandBufferErrorDomain code:MTLCommandBufferErrorInternal userInfo:@{NSLocalizedDescriptionKey:[NSString stringWithUTF8String:message]}];}
}
- (void)executeCommitted {
 unsigned pending=_overlap?_queue->_pending.fetch_sub(1):0;
 if(_frameTrace){_flightTrace.submitQueue=fuxi::frameNanos(_commitReturn);_flightTrace.workerDepth=pending;}
 bool backboard=!strcmp(clientProcess,"backboardd");_gpuBefore=backboard?fuxi::Backend::gpuNanos.load():0;
 auto before=fuxi::frameCounts;
 {fuxi::FrameTraceScope scope((_frameTrace || _overlap)?&_flightTrace:nullptr);
 try {
  if(_status==MTLCommandBufferStatusError)throw std::runtime_error("command snapshot failed");
  if(backboardSurvey.active() && fuxi::backboardGate.blocked()){_scheduled=YES;}
  else {
  if(fuxi::backboardGate.blocked())throw std::runtime_error("backboardd failure latched: CPU on next respawn");
  if(!strcmp(clientProcess,"fuxi-ca-smoke") && failureCount.load())throw std::runtime_error("CA diagnostic failure latched: GPU submission refused");
  if(_rejected)throw std::runtime_error("unsupported command: GPU submission refused");
  if(clientMode==fuxi::ClientMode::census)throw std::runtime_error("census: no GPU submission");
  auto *backend=owner(_queue->_device)->backend;
  if(gpuScanout)backend->requireLive();
  auto leaseStart=fuxi::FrameClock::now();
  _lease=std::make_shared<fuxi::SurfaceLease>(gpuScanout,lockScanoutSurface,unlockScanoutSurface,&backend->poisoned,_overlap);
  if(gpuScanout)for(auto &entry:commandSurfaces(self))_lease->add(entry.first,entry.second);
  if(_frameTrace)fuxi::addLeaseTrace(_flightTrace,*_lease,fuxi::frameNanos(leaseStart));
  auto refresh=[&]{auto start=fuxi::FrameClock::now();
   uint64_t bytes=_overlap?_shadows.apply(_queue->_shadowPool,[&](void *key)->void *{for(auto &entry:_shadowUses)if(entry.first->shadowKey()==key)return entry.first->contents;return key;}):refreshCommandShadows(self);if(_frameTrace)_flightTrace.shadowCopyBytes+=bytes;
   auto ns=fuxi::frameNanos(start);fuxi::Backend::shadowCopyNanos+=ns;if(_frameTrace)_flightTrace.shadowCopy+=ns;
  };
  std::function<void()> prepare;std::vector<fuxi::PrepareWrite> prepareWrites;const std::vector<fuxi::PrepareWrite> *declared=nullptr;
  if(_overlap){if(_shadows.bytes()){prepare=refresh;bool known=true; // the apply() destinations, resolved to backend buffers; unresolved means undeclared (full drain)
   _shadows.eachWrite([&](void *destination,uint64_t offset,uint64_t bytes){fuxi::Buffer *target=nullptr;for(auto &entry:_shadowUses)if(entry.first->shadowKey()==destination){target=entry.first;break;}if(target)prepareWrites.push_back({target,offset,bytes});else known=false;});
   if(known)declared=&prepareWrites;}}else refresh();
  size_t begin=0;
  auto scheduled=[&]{@synchronized(self){_scheduled=YES;if(_status<MTLCommandBufferStatusScheduled)_status=MTLCommandBufferStatusScheduled;}};
  for(const auto &point:_events){
   if(point.operation>begin){backend->submit(std::vector<fuxi::Operation>(_ops.begin()+begin,_ops.begin()+point.operation),scheduled,nullptr,prepare,declared);prepare={};declared=nullptr;begin=point.operation;}
   if(point.signal)[point.event setSignaledValue:point.value];else if(![point.event waitUntilSignaledValue:point.value timeoutMS:2000])throw std::runtime_error("process-local event wait timed out");
  }
  // FUXI_MTL_EARLY_RELEASE: on the retire queue, after a signaled fence and before the backend mutex (held by the next encode).
  std::function<void()> fenced;if(_overlap && fuxi::systemTranslationPolicy().earlyRelease)fenced=[self]{bool failed;@synchronized(self){failed=_status==MTLCommandBufferStatusError;}if(!failed){_lease.reset();_pendingLease.reset();}};
  if(_events.empty())backend->submit(_ops,scheduled,_overlap?&_retirement:nullptr,prepare,declared,fenced);
  else if(begin<_ops.size())backend->submit(std::vector<fuxi::Operation>(_ops.begin()+begin,_ops.end()),scheduled,_overlap?&_retirement:nullptr,prepare,declared,fenced);
  else {if(prepare)backend->submit({},scheduled,nullptr,prepare,declared);else scheduled();}
  if(backboard){std::lock_guard<std::mutex> lock(backend->mutex);
   for(id r in _resources){if(![r isKindOfClass:[FuxiTexture class]] || ![(FuxiTexture *)r iosurface])continue;auto *texture=((FuxiTexture *)r)->_texture;if(!texture)continue;
    for(const auto &op:_ops)if(op.pipeline || op.clearOnly)for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);if(c.texture==texture && c.store && texture->isDefined(c.level))_surfaceFrame=true;}}
  }
  }
 }catch(const std::exception &e){[self submissionError:e.what()];}
 if(_frameTrace){_flightTrace.counts.pipelines+=fuxi::frameCounts.pipelines-before.pipelines;_flightTrace.counts.descriptors+=fuxi::frameCounts.descriptors-before.descriptors;_flightTrace.counts.allocations+=fuxi::frameCounts.allocations-before.allocations;}
 }
 if(!_overlap && _status!=MTLCommandBufferStatusError)_status=MTLCommandBufferStatusCompleted;
 if(_overlap){
  // Start the fence wait before user scheduled handlers. Retirement can free a
  // flight slot even if a handler enqueues another command on this queue.
  auto queued=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
  dispatch_async(_queue->_retireQueue,^{@autoreleasepool{if(_frameTrace)_flightTrace.retireQueue=fuxi::frameNanos(queued);[self retireCommitted];}});
  auto start=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};[self publishScheduled];if(_frameTrace)_flightTrace.scheduledHandlers=fuxi::frameNanos(start);dispatch_group_leave(_scheduledGroup);
 }
 else [self retireCommitted];
}
- (void)retireCommitted {
 {fuxi::FrameTraceScope scope((_frameTrace || _overlap)?&_flightTrace:nullptr);
 try{if(_retirement){_retirement();_retirement={};}}catch(const std::exception &e){[self submissionError:e.what()];}
 auto releaseStart=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
 if(_pendingLease && _status==MTLCommandBufferStatusError)_pendingLease->quarantine();
 _lease.reset();_pendingLease.reset();if(_frameTrace)_flightTrace.surfaceRelease=fuxi::frameNanos(releaseStart); // Never hand scanout a surface before its fence; poison keeps it locked.
 if(_status!=MTLCommandBufferStatusError){
  for(const auto &op:_ops)if(op.pipeline)completedDraws.fetch_add(1);
 }
 if(backboardSurvey.active() && fuxi::backboardGate.blocked()){
  _scheduled=YES;_status=MTLCommandBufferStatusCompleted;[_error release];_error=nil;for(const auto &point:_events)if(point.signal)[point.event setSignaledValue:point.value];finishSurvey();
 }
 _flightTrace.wall=fuxi::frameNanos(_commitStart);
 if(!_overlap && _surfaceFrame && _status!=MTLCommandBufferStatusError)fuxi::backboardGate.completed((_frameTrace || _overlap)?_flightTrace.gpuExec:fuxi::Backend::gpuNanos.load()-_gpuBefore,_flightTrace.wall/1e6,_frameTrace?&_flightTrace:nullptr);
 }
 if(_overlap){
  --_queue->_depth;dispatch_semaphore_signal(_queue->_inflight);
  dispatch_async(_queue->_finishQueue,^{@autoreleasepool{[self finishCommitted];}});
 }else [self finishCommitted];
}
- (void)finishCommitted {
 if(!_overlap)[self publishScheduled];
 if(_overlap){
  dispatch_group_wait(_scheduledGroup,DISPATCH_TIME_FOREVER);
  _flightTrace.completionLatency=fuxi::frameNanos(_commitStart);
  if(_surfaceFrame && _status!=MTLCommandBufferStatusError)fuxi::backboardGate.completed(_flightTrace.gpuExec,_flightTrace.wall/1e6,_frameTrace?&_flightTrace:nullptr);
 }
 @synchronized(self){_finished=YES;if(_status!=MTLCommandBufferStatusError)_status=MTLCommandBufferStatusCompleted;
  NSArray *handlers=[_handlers copy];[_handlers removeAllObjects];[self deliverCompletedHandlers:handlers];[handlers release];}
 @synchronized(self){[_scheduledHandlers removeAllObjects];}[_resources removeAllObjects];_resourceCache.clear();_resourceSet.clear();_sampledRes.clear();_otherRes.clear();_queue->_operationStorage.release(_ops);_uploads.release(_queue->_uploadStorage,owner(_queue->_device)->backend && !owner(_queue->_device)->backend->poisoned,[](FuxiBuffer *b){[b release];});_events.clear();_shadows.release(_queue->_shadowPool);_shadowUses.clear();
 if(_overlap)dispatch_group_leave(_completionGroup);
}
- (void)waitUntilCompleted {CENSUS();if(completionCommand!=self){auto start=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};dispatch_group_wait(_completionGroup,DISPATCH_TIME_FOREVER);if(_frameTrace)_queue->_traceWaitCompleted=std::max<uint64_t>(1,fuxi::frameNanos(start));}}
- (void)waitUntilScheduled {CENSUS();if(!_overlap)return;auto start=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
 bool wait=false;@synchronized(self){wait=!_earlySubmit && !_scheduled && !_finished && _status!=MTLCommandBufferStatusError;}
 if(wait)dispatch_group_wait(_scheduledGroup,DISPATCH_TIME_FOREVER);
 if(_frameTrace)_queue->_traceWaitScheduled=std::max<uint64_t>(1,fuxi::frameNanos(start));
}
- (BOOL)commitAndWaitUntilSubmitted {CENSUS();[self commit];[self waitUntilScheduled];return (_earlySubmit || _scheduled) && self.status!=MTLCommandBufferStatusError;}
- (CFTimeInterval)kernelStartTime {FRONTEND_TIME(self);CENSUS();return 0;}
- (CFTimeInterval)kernelEndTime {FRONTEND_TIME(self);CENSUS();return 0;}
- (CFTimeInterval)GPUStartTime {FRONTEND_TIME(self);CENSUS();return 0;}
- (CFTimeInterval)GPUEndTime {FRONTEND_TIME(self);CENSUS();return 0;}
- (void)pushDebugGroup:(NSString *)name {FRONTEND_TIME(self);CENSUS();(void)name;}
- (void)popDebugGroup {FRONTEND_TIME(self);CENSUS();}
- (void)encodeCacheHintTag:(uint64_t)tag resourceGroups:(const id __unsafe_unretained [])groups count:(NSUInteger)count {FRONTEND_TIME(self);CENSUS();(void)tag;
 if(count>256 || (count && !groups)){[self rejectSelector:_cmd];return;}
 for(NSUInteger i=0;i<count;++i)if(groups[i])[_resources addObject:groups[i]];
}
- (void)encodeCacheHintFinalize:(uint64_t)tag resourceGroups:(const id __unsafe_unretained [])groups count:(NSUInteger)count {FRONTEND_TIME(self);
 [self encodeCacheHintTag:tag resourceGroups:groups count:count];
}
- (void)reserveOverlap {
 auto start=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};
 if(!_earlySubmit){dispatch_semaphore_wait(_queue->_inflight,DISPATCH_TIME_FOREVER);if(_frameTrace)_flightTrace.admissionWait+=fuxi::frameNanos(start);_flightTrace.inflight=++_queue->_depth;}
 _enqueued=YES;dispatch_group_enter(_commitReady);++_queue->_pending;
 dispatch_async(_queue->_submitQueue,^{@autoreleasepool{
  dispatch_group_wait(_commitReady,DISPATCH_TIME_FOREVER);
  if(_earlySubmit){auto start=_frameTrace?fuxi::FrameClock::now():fuxi::FrameClock::time_point{};dispatch_semaphore_wait(_queue->_inflight,DISPATCH_TIME_FOREVER);if(_frameTrace)_flightTrace.admissionWait+=fuxi::frameNanos(start);_flightTrace.inflight=++_queue->_depth;}
  [self executeCommitted];
 }});
}
// Explicit enqueue reserves submission/completion order before commit in overlap mode.
- (void)enqueue {FRONTEND_TIME(self);FUXI_PHASE(cbMisc);CENSUS();if(_enqueued || _status!=MTLCommandBufferStatusNotEnqueued){[self rejectSelector:_cmd];return;}if(_overlap)[self reserveOverlap];else _enqueued=YES;}
// Protected content is unsupported: only the "none" option is accepted.
- (NSUInteger)protectionOptions {FRONTEND_TIME(self);CENSUS();return 0;}
- (void)setProtectionOptions:(NSUInteger)o {FRONTEND_TIME(self);CENSUS();if(o)[self rejectSelector:_cmd];}
@end
@implementation FuxiBlit
- (void)rejectSelector:(SEL)s {FRONTEND_TIME(_command);unsupported(self,s);[_command rejectSelector:s];}
- (void)dealloc {[_command release];[super dealloc];}
- (id)device {FRONTEND_TIME(_command);FUXI_PHASE(blit);CENSUS();return _command->_queue->_device;}
- (FuxiBuffer *)validate:(id)b offset:(NSUInteger)o size:(NSUInteger)n {FRONTEND_TIME(_command);FUXI_PHASE(blit);CENSUS();
 if(_ended || !_command->_encoding || _command->_status!=MTLCommandBufferStatusNotEnqueued) invalid(@"inactive encoder");
 if(![b isKindOfClass:[FuxiBuffer class]] || [b device]!=_command->_queue->_device) invalid(@"foreign buffer/device");
 FuxiBuffer *buffer=b;
 if(o>buffer->_buffer->length || n>buffer->_buffer->length-o || ((o|n)&3)) invalid(@"M4 transfers require in-bounds 4-byte aligned offset and length");
 return buffer;
}
- (void)fillBuffer:(id<MTLBuffer>)b range:(NSRange)r value:(uint8_t)v {FRONTEND_TIME(_command);FUXI_PHASE(blit);@try {CENSUS();
 FuxiBuffer *dst=[self validate:b offset:r.location size:r.length];if(!r.length)return;
 retainCommandResource(_command,dst,false,fuxi::ShadowFullReason::blit);_command->_ops.push_back({nullptr,dst->_buffer,0,r.location,r.length,v});
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)copyFromBuffer:(id<MTLBuffer>)s sourceOffset:(NSUInteger)so toBuffer:(id<MTLBuffer>)d destinationOffset:(NSUInteger)to size:(NSUInteger)n {FRONTEND_TIME(_command);FUXI_PHASE(blit);@try {CENSUS();
 FuxiBuffer *src=[self validate:s offset:so size:n],*dst=[self validate:d offset:to size:n];
 if(src==dst && n && so<to+n && to<so+n) invalid(@"overlapping copy");
 if(!n)return;
 retainCommandResource(_command,src,false,fuxi::ShadowFullReason::blit);retainCommandResource(_command,dst,false,fuxi::ShadowFullReason::blit);
 _command->_ops.push_back({src->_buffer,dst->_buffer,so,to,n,0});
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (FuxiTexture *)texture:(id)t slice:(NSUInteger)s level:(NSUInteger)l origin:(MTLOrigin)o size:(MTLSize)z {FRONTEND_TIME(_command);FUXI_PHASE(blit);
 if(_ended || !_command->_encoding || _command->_status!=MTLCommandBufferStatusNotEnqueued)invalid(@"inactive blit encoder");
 if(![t isKindOfClass:[FuxiTexture class]] || [t device]!=[self device])invalid(@"foreign texture/device");
 bool volume=[t textureType]==MTLTextureType3D;NSUInteger levelDepth=l<64?std::max(NSUInteger(1),[t depth]>>l):1;
 if(s || l>=[t mipmapLevelCount] || (volume?(!z.depth || o.z>levelDepth || z.depth>levelDepth-o.z):(o.z || z.depth!=1)) || !z.width || !z.height || o.x>std::max(NSUInteger(1),[t width]>>l) || z.width>std::max(NSUInteger(1),[t width]>>l)-o.x || o.y>std::max(NSUInteger(1),[t height]>>l) || z.height>std::max(NSUInteger(1),[t height]>>l)-o.y)invalid(@"texture copy region bounds");return t;
}
- (FuxiBuffer *)imageBuffer:(id)b offset:(NSUInteger)offset row:(NSUInteger)row image:(NSUInteger)image size:(MTLSize)size pixel:(NSUInteger)pixel {FRONTEND_TIME(_command);FUXI_PHASE(blit);
 if(![b isKindOfClass:[FuxiBuffer class]] || [b device]!=[self device] || row<size.width*pixel || row%pixel || row>fuxi::Backend::maxBufferLength/size.height || (image && image<row*size.height) || (size.depth>1 && (!image || image%row || image>fuxi::Backend::maxBufferLength)) || offset%4 || offset%pixel)invalid(@"texture buffer stride/alignment");
 size_t bytes=(size.depth-1)*(size.depth>1?image:0)+(size.height-1)*row+size.width*pixel;
 if(offset>[b length] || bytes>[b length]-offset)invalid(@"texture buffer bounds");return b;
}
- (void)copyFromTexture:(id)t sourceSlice:(NSUInteger)s sourceLevel:(NSUInteger)l sourceOrigin:(MTLOrigin)o sourceSize:(MTLSize)z
 toBuffer:(id)b destinationOffset:(NSUInteger)offset destinationBytesPerRow:(NSUInteger)row destinationBytesPerImage:(NSUInteger)image {FRONTEND_TIME(_command);@try {CENSUS();
 FuxiTexture *src=[self texture:t slice:s level:l origin:o size:z];
 FuxiBuffer *dst=[self imageBuffer:b offset:offset row:row image:image size:z pixel:fuxi::Backend::texelBytes((uint32_t)[src pixelFormat])];
 fuxi::Operation op{};op.texture=src->_texture;op.srcLevel=src->_baseLevel+(uint32_t)l;op.dst=dst->_buffer;op.to=offset;op.srcX=o.x;op.srcY=o.y;op.srcZ=o.z;op.width=z.width;op.height=z.height;op.depth=z.depth;op.rowBytes=row;op.imageBytes=z.depth>1?image:0;
 retainCommandResource(_command,src,false,fuxi::ShadowFullReason::blit);retainCommandResource(_command,dst,false,fuxi::ShadowFullReason::blit);_command->_ops.push_back(op);
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)copyFromBuffer:(id)b sourceOffset:(NSUInteger)offset sourceBytesPerRow:(NSUInteger)row sourceBytesPerImage:(NSUInteger)image
 sourceSize:(MTLSize)z toTexture:(id)t destinationSlice:(NSUInteger)s destinationLevel:(NSUInteger)l destinationOrigin:(MTLOrigin)o {FRONTEND_TIME(_command);@try {CENSUS();
 if(caTrace())fprintf(stderr,"FUXI MTL: trace bufferToTexture offset %lu row %lu %lux%lu to %lu,%lu format %lu\n",(unsigned long)offset,(unsigned long)row,(unsigned long)z.width,(unsigned long)z.height,(unsigned long)o.x,(unsigned long)o.y,(unsigned long)[t pixelFormat]);
 FuxiTexture *dst=[self texture:t slice:s level:l origin:o size:z];
 FuxiBuffer *src=[self imageBuffer:b offset:offset row:row image:image size:z pixel:fuxi::Backend::texelBytes((uint32_t)[dst pixelFormat])];
 fuxi::Operation op{};op.src=src->_buffer;op.from=offset;op.texture=dst->_texture;op.dstLevel=dst->_baseLevel+(uint32_t)l;op.upload=true;op.dstX=o.x;op.dstY=o.y;op.dstZ=o.z;op.width=z.width;op.height=z.height;op.depth=z.depth;op.rowBytes=row;op.imageBytes=z.depth>1?image:0;
 retainCommandResource(_command,src,false,fuxi::ShadowFullReason::blit);retainCommandResource(_command,dst,false,fuxi::ShadowFullReason::blit);_command->_ops.push_back(op);
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)copyFromTexture:(id)t sourceSlice:(NSUInteger)s sourceLevel:(NSUInteger)l sourceOrigin:(MTLOrigin)o sourceSize:(MTLSize)z
 toTexture:(id)d destinationSlice:(NSUInteger)ds destinationLevel:(NSUInteger)dl destinationOrigin:(MTLOrigin)dest {FRONTEND_TIME(_command);@try {CENSUS();
 FuxiTexture *src=[self texture:t slice:s level:l origin:o size:z],*dst=[self texture:d slice:ds level:dl origin:dest size:z];
 if([src pixelFormat]!=[dst pixelFormat] || [src textureType]!=[dst textureType])invalid(@"texture copy format/type mismatch");
 // Backend snapshots aliased image regions through a bounded transfer buffer.
 // Even disjoint rectangles can share a Vulkan image subresource or allocation.
 fuxi::Operation op{};op.sourceTexture=src->_texture;op.texture=dst->_texture;op.srcLevel=src->_baseLevel+(uint32_t)l;op.dstLevel=dst->_baseLevel+(uint32_t)dl;op.srcX=o.x;op.srcY=o.y;op.srcZ=o.z;op.dstX=dest.x;op.dstY=dest.y;op.dstZ=dest.z;op.width=z.width;op.height=z.height;op.depth=z.depth;
 retainCommandResource(_command,src,false,fuxi::ShadowFullReason::blit);retainCommandResource(_command,dst,false,fuxi::ShadowFullReason::blit);_command->_ops.push_back(op);
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
// Ordinary color transfers have no depth/stencil or row-linear PVRTC option.
- (void)copyFromBuffer:(id<MTLBuffer>)b sourceOffset:(NSUInteger)offset sourceBytesPerRow:(NSUInteger)row sourceBytesPerImage:(NSUInteger)image sourceSize:(MTLSize)z
 toTexture:(id<MTLTexture>)t destinationSlice:(NSUInteger)s destinationLevel:(NSUInteger)l destinationOrigin:(MTLOrigin)o options:(MTLBlitOption)options {FRONTEND_TIME(_command);
 if(options!=MTLBlitOptionNone){[self rejectSelector:_cmd];return;}
 [self copyFromBuffer:b sourceOffset:offset sourceBytesPerRow:row sourceBytesPerImage:image sourceSize:z toTexture:t destinationSlice:s destinationLevel:l destinationOrigin:o];
}
- (void)copyFromTexture:(id<MTLTexture>)t sourceSlice:(NSUInteger)s sourceLevel:(NSUInteger)l sourceOrigin:(MTLOrigin)o sourceSize:(MTLSize)z
 toBuffer:(id<MTLBuffer>)b destinationOffset:(NSUInteger)offset destinationBytesPerRow:(NSUInteger)row destinationBytesPerImage:(NSUInteger)image options:(MTLBlitOption)options {FRONTEND_TIME(_command);
 if(options!=MTLBlitOptionNone){[self rejectSelector:_cmd];return;}
 [self copyFromTexture:t sourceSlice:s sourceLevel:l sourceOrigin:o sourceSize:z toBuffer:b destinationOffset:offset destinationBytesPerRow:row destinationBytesPerImage:image];
}
- (void)copyFromTexture:(id<MTLTexture>)src toTexture:(id<MTLTexture>)dst {FRONTEND_TIME(_command);FUXI_PHASE(blit);@try {CENSUS();
 (void)[self texture:src slice:0 level:0 origin:MTLOriginMake(0,0,0) size:MTLSizeMake(1,1,1)];
 (void)[self texture:dst slice:0 level:0 origin:MTLOriginMake(0,0,0) size:MTLSizeMake(1,1,1)];
 if(src.width!=dst.width || src.height!=dst.height || src.depth!=dst.depth || src.textureType!=dst.textureType || src.mipmapLevelCount!=dst.mipmapLevelCount || src.pixelFormat!=dst.pixelFormat)invalid(@"whole texture copy shape/format");
 for(NSUInteger l=0;l<src.mipmapLevelCount;++l)[self copyFromTexture:src sourceSlice:0 sourceLevel:l sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(std::max(NSUInteger(1),src.width>>l),std::max(NSUInteger(1),src.height>>l),std::max(NSUInteger(1),src.depth>>l)) toTexture:dst destinationSlice:0 destinationLevel:l destinationOrigin:MTLOriginMake(0,0,0)];
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)fence:(id)f {FRONTEND_TIME(_command);FUXI_PHASE(blit);CENSUS();
 if(_ended || !_command->_encoding || ![f isKindOfClass:[FuxiFence class]] || [f device]!=[self device]){[self rejectSelector:_cmd];return;}retainCommandResource(_command,f);
}
- (void)updateFence:(id<MTLFence>)f {FRONTEND_TIME(_command);FUXI_PHASE(blit);[self fence:f];}
- (void)waitForFence:(id<MTLFence>)f {FRONTEND_TIME(_command);FUXI_PHASE(blit);[self fence:f];}
- (void)synchronizeResource:(id<MTLResource>)r {FRONTEND_TIME(_command);FUXI_PHASE(blit);CENSUS();
 if(_ended || !_command->_encoding || _command->_status!=MTLCommandBufferStatusNotEnqueued || ![r isKindOfClass:[FuxiResource class]] || [r device]!=[self device]){[self rejectSelector:_cmd];return;}if(r)retainCommandResource(_command,r,false,fuxi::ShadowFullReason::blit);
}
- (void)synchronizeTexture:(id<MTLTexture>)t slice:(NSUInteger)s level:(NSUInteger)l {FRONTEND_TIME(_command);FUXI_PHASE(blit);@try {CENSUS();
 (void)[self texture:t slice:s level:l origin:MTLOriginMake(0,0,0) size:MTLSizeMake(1,1,1)];
 // iOS shared resources have no separate managed CPU copy. Submission already
 // flushes/invalidate host mappings and waits for its fence before completion.
 retainCommandResource(_command,t);
} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
- (void)generateMipmapsForTexture:(id<MTLTexture>)t {FRONTEND_TIME(_command);FUXI_PHASE(blit);CENSUS();
 if(_ended || !_command->_encoding || _command->_status!=MTLCommandBufferStatusNotEnqueued || ![t isKindOfClass:[FuxiTexture class]] || [t device]!=[self device]){[self rejectSelector:_cmd];return;}retainCommandResource(_command,t);
 if([t mipmapLevelCount]>1){auto *texture=(FuxiTexture *)t;fuxi::Operation op{};op.texture=texture->_texture;op.generateMips=true;op.mipBase=texture->_baseLevel;op.mipCount=(uint32_t)[texture mipmapLevelCount];_command->_ops.push_back(op);}
}
- (void)endEncoding {FRONTEND_TIME(_command);FUXI_PHASE(blit);@try {CENSUS();if(_ended)invalid(@"endEncoding twice");_ended=YES;_command->_encoding=NO;} @catch(NSException *e) {(void)e;[self rejectSelector:_cmd];}}
@end
#undef FRONTEND_TIME
/* Methods are copied onto a runtime subclass. No fixed private layout/ivars are
 * read, initialized, or freed through the private superclass implementation. */
static NSString *surfaceDescriptorText(IOSurfaceRef surface,NSUInteger plane){
 if(!surface)return @"surface=absent";
 NSMutableString *text=[NSMutableString stringWithFormat:@"surface=fourcc0x%08x/plane%lu/planes%lu/size%lux%lu/row%lu/bytes%lu",IOSurfaceGetPixelFormat(surface),(unsigned long)plane,
  (unsigned long)IOSurfaceGetPlaneCount(surface),(unsigned long)IOSurfaceGetWidth(surface),(unsigned long)IOSurfaceGetHeight(surface),(unsigned long)IOSurfaceGetBytesPerRow(surface),(unsigned long)IOSurfaceGetAllocSize(surface)];
 for(size_t i=0;i<std::min<size_t>(IOSurfaceGetPlaneCount(surface),8);++i){
  uintptr_t base=(uintptr_t)IOSurfaceGetBaseAddress(surface),start=(uintptr_t)IOSurfaceGetBaseAddressOfPlane(surface,i);
  [text appendFormat:@" p%lu=size%lux%lu/element%lu/row%lu/offset%llu",(unsigned long)i,(unsigned long)IOSurfaceGetWidthOfPlane(surface,i),(unsigned long)IOSurfaceGetHeightOfPlane(surface,i),
   (unsigned long)IOSurfaceGetBytesPerElementOfPlane(surface,i),(unsigned long)IOSurfaceGetBytesPerRowOfPlane(surface,i),(unsigned long long)(start>=base?start-base:UINT64_MAX)];
 }
 return text;
}
@interface FuxiDeviceMethods : FuxiObject
@end
@implementation FuxiDeviceMethods
- (id)init {CENSUS();unsupported(self,_cmd);[self release];return nil;}
- (id)initWithAcceleratorPort:(uint32_t)port {
 checkClient();
 if(clientMode==fuxi::ClientMode::deny) {[self release];return nil;}
 CENSUS();
 if((clientMode!=fuxi::ClientMode::census && geteuid()!=0 && strcmp(clientProcess,"backboardd")) ||
    fuxi::backboardGate.blocked() || !port || owner(self)) {[self release];return nil;}
 if(clientMode==fuxi::ClientMode::census) {
  FuxiOwner *state=[FuxiOwner new];
  objc_setAssociatedObject(self,&ownerKey,state,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[state release];return self;
 }
#ifndef FUXI_MTL_REGISTRY_ID
 void *io=dlopen("/System/Library/Frameworks/IOKit.framework/IOKit",RTLD_NOW|RTLD_LOCAL);
 auto getID=io?(int(*)(uint32_t,uint64_t *))dlsym(io,"IORegistryEntryGetRegistryEntryID"):nullptr;
 uint64_t registry=0;int rc=getID?getID(port,&registry):-1;
 if(io)dlclose(io);
#else
 uint64_t registry=0;int rc=FUXI_MTL_REGISTRY_ID(port,&registry);
#endif
 if(rc || !registry) {failureLog("FUXI MTL: registryID FAIL\n");[self release];return nil;}
 if(selectedBackend==fuxi::MetalBackend::native){
  if(!FUXI_MTL_NATIVE_PREFLIGHT(port)){
   failureLog("FUXI MTL: NativeMetal host-import ABI-2 preflight FAIL\n");[self release];return nil;
  }
  static const FuxiNativeHooks hooks={FUXI_NATIVE_ABI,sizeof(FuxiNativeHooks),
   [](const char *why){failureLog("FUXI MTL: native refusal %s\n",why?why:"unknown");},
   [](){return fuxi::backboardGate.blocked();},
   [](uint64_t gpu,double wall,uint64_t draws,bool storedSurface){
    if(fuxi::backboardGate.blocked())return;
    completedDraws.fetch_add(draws);if(gpu){fuxi::Backend::gpuNanos.fetch_add(gpu);++fuxi::Backend::gpuSamples;}
    if(storedSurface)fuxi::backboardGate.completed(gpu,wall);
    if(fuxi::backboardGate.blocked())failureLog("FUXI MTL: native completion latch\n");}};
  id device=nil;
  try { @try {device=FUXI_MTL_NATIVE_OPEN(clientProcess,registry,&hooks);}
  @catch(NSException *e){failureLog("FUXI MTL: native open exception %s\n",e.reason.UTF8String);} }
  catch(const std::exception &e){failureLog("FUXI MTL: native open exception %s\n",e.what());}
  if(!device)failureLog("FUXI MTL: native device unavailable\n");
  [self release];return [device retain];
 }
 FuxiOwner *state=[FuxiOwner new];state->registry=registry;
 try {if(clientMode!=fuxi::ClientMode::census)state->backend=new fuxi::Backend(clientProcess);}
 catch(const std::exception &e) {failureLog("FUXI MTL: device FAIL %s\n",e.what());[state release];[self release];return nil;}
 // CA's documented late library/pipeline failures can abort, rather than fall
 // back. Refuse discovery on the current no-import kernel even after a root
 // allow-list edit; no partially usable CA device enters Metal's device cache.
 if((!strcmp(clientProcess,"backboardd") || !strcmp(clientProcess,"SpringBoard")) && !state->backend->supportsHostImport()) {
  fprintf(stderr,"FUXI MTL: backboardd gate deny reason=host-import-unavailable\n");
  unsupported(self,@selector(newTextureWithDescriptor:iosurface:plane:));
  fprintf(stderr,"FUXI MTL: CA preflight deny IOSurface HostImport unavailable\n");[state release];[self release];return nil;
 }
 objc_setAssociatedObject(self,&ownerKey,state,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[state release];return self;
}
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wobjc-missing-super-calls"
- (void)dealloc {
 objc_setAssociatedObject(self,&ownerKey,nil,OBJC_ASSOCIATION_RETAIN_NONATOMIC);
 // Private base was never initialized. NSObject disposal only.
 object_dispose(self);
}
#pragma clang diagnostic pop
- (NSString *)name {CENSUS();return @"Fuxi Turnip Adreno 740 (M7 bounded offscreen)";}
- (uint64_t)registryID {CENSUS();return owner(self)->registry;}
- (NSUInteger)maxBufferLength {CENSUS();return fuxi::Backend::maxBufferLength;}
- (uint64_t)dedicatedMemorySize {CENSUS();return 0;}
- (uint64_t)sharedMemorySize {CENSUS();return NSProcessInfo.processInfo.physicalMemory;}
- (uint64_t)recommendedMaxWorkingSetSize {CENSUS();return std::min<uint64_t>(fuxi::ResourceLimits::budget,[self sharedMemorySize]);}
- (NSUInteger)currentAllocatedSize {CENSUS();auto *state=owner(self);return state && state->backend?state->backend->resourceBytes():0;}
- (BOOL)hasUnifiedMemory {CENSUS();return YES;}
- (BOOL)isLowPower {CENSUS();return NO;}
- (BOOL)isHeadless {CENSUS();return YES;}
- (BOOL)isRemovable {CENSUS();return NO;}
- (BOOL)supportsFeatureSet:(NSUInteger)s {CENSUS();(void)s;return NO;}
- (BOOL)supportsFamily:(MTLGPUFamily)f {CENSUS();(void)f;return NO;}
- (BOOL)supportsTextureSampleCount:(NSUInteger)n {CENSUS();return n==1;}
- (BOOL)supportsRaytracing {CENSUS();return NO;}
- (BOOL)supportsFunctionPointers {CENSUS();return NO;}
- (BOOL)supportsDynamicLibraries {CENSUS();return NO;}
- (BOOL)supportsPullModelInterpolation {CENSUS();return NO;}
- (BOOL)supportsRasterOrderGroups {CENSUS();return NO;}
- (BOOL)areProgrammableSamplePositionsSupported {CENSUS();return NO;}
- (BOOL)areBarycentricCoordsSupported {CENSUS();return NO;}
- (BOOL)areRasterOrderGroupsSupported {CENSUS();return NO;}
- (MTLReadWriteTextureTier)readWriteTextureSupport {CENSUS();return MTLReadWriteTextureTierNone;}
- (MTLSize)maxThreadsPerThreadgroup {CENSUS();return MTLSizeMake(0,0,0);}
// Private scalar queries established by the repo's QuartzCore call-site notes.
// These are limits of this bounded path, never an Apple family emulation table.
- (BOOL)supportsMemorylessRenderTargets {CENSUS();return NO;}
- (BOOL)supportsYCBCRPackedFormats12 {CENSUS();return NO;}
- (NSUInteger)minConstantBufferAlignmentBytes {CENSUS();return 256;}
// QuartzCore sizes linear image uploads by these (M9 CA smoke stage 3). 16 bytes is
// Apple's linear texture alignment and metal2vulkan's DeviceLinearTextureAlignmentBytes.
- (NSUInteger)deviceLinearReadOnlyTextureAlignmentBytes {CENSUS();return 16;}
- (NSUInteger)minimumLinearTextureAlignmentForPixelFormat:(MTLPixelFormat)format {CENSUS();(void)format;return 16;}
- (NSUInteger)minimumTextureBufferAlignmentForPixelFormat:(MTLPixelFormat)format {CENSUS();(void)format;return 16;}
- (NSUInteger)iosurfaceReadOnlyTextureAlignmentBytes {CENSUS();return 256;}
- (NSUInteger)maxFragmentTextures {CENSUS();return fuxi::resourceSlots;}
- (NSUInteger)maxFragmentSamplers {CENSUS();return fuxi::resourceSlots;}
- (NSUInteger)maxTextureWidth2D {CENSUS();return owner(self)->backend?owner(self)->backend->maxTextureDimension():0;}
- (NSUInteger)maxTextureHeight2D {CENSUS();return owner(self)->backend?owner(self)->backend->maxTextureDimension():0;}
- (NSUInteger)maxColorAttachments {CENSUS();return 1;}
- (NSString *)vendorName {CENSUS();return @"Qualcomm";}
- (BOOL)supportsYCBCRFormats {CENSUS();return NO;}
- (BOOL)supportsYCBCRFormats12 {CENSUS();return NO;}
// iOS 16.7 CA uses this scalar to select lph shader names. A740 has native
// FP16; this does not grant an Apple family or any tile/fetch/format feature.
- (BOOL)supportsNativeHardwareFP16 {CENSUS();return YES;}
- (BOOL)supportsTileShaders {CENSUS();return NO;}
- (BOOL)supportsSIMDGroup {CENSUS();return NO;}
- (id)newCommandQueueWithMaxCommandBufferCount:(NSUInteger)n {CENSUS();
 if(!n || n>64) {unsupported(self,_cmd);return nil;}FuxiQueue *q=[self newCommandQueue];if(n==1 && q && q->_inflight){dispatch_release(q->_inflight);q->_inflight=dispatch_semaphore_create(1);}return q;
}
- (id)newCommandQueue {CENSUS();if(clientMode==fuxi::ClientMode::census)return nil;FuxiQueue *q=[FuxiQueue new];q->_device=[self retain];q->_depth=0;q->_pending=0;q->_traceReturn=0;q->_traceDelivery=0;q->_traceHandlers=0;q->_traceWaitCompleted=0;q->_traceWaitScheduled=0;if(fuxi::systemTranslationPolicy().overlap){
 // The compositor's submit/retire/handler work is on the frame path; at default
 // QoS it competes with background daemons (UniXNU AdrenoMetal c6d29ca does the same).
 dispatch_queue_attr_t attr=dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL,compositorQoS(),0);
 q->_submitQueue=dispatch_queue_create("fuxi.metal.submit",attr);q->_retireQueue=dispatch_queue_create("fuxi.metal.retire",attr);q->_finishQueue=dispatch_queue_create("fuxi.metal.finish",attr);q->_deliveryQueue=dispatch_queue_create("fuxi.metal.handlers",attr);q->_inflight=dispatch_semaphore_create(2);
 if(!q->_submitQueue || !q->_retireQueue || !q->_finishQueue || !q->_deliveryQueue || !q->_inflight){[q release];return nil;}
 }return q;}
- (id)newHeapWithDescriptor:(MTLHeapDescriptor *)d {CENSUS();
 if(!d || d.type!=MTLHeapTypeAutomatic || !d.size || d.size>fuxi::Backend::maxBufferLength || (d.storageMode!=MTLStorageModeShared && d.storageMode!=MTLStorageModePrivate) || (d.cpuCacheMode!=MTLCPUCacheModeDefaultCache && d.cpuCacheMode!=MTLCPUCacheModeWriteCombined) ||
    (d.hazardTrackingMode!=MTLHazardTrackingModeDefault && d.hazardTrackingMode!=MTLHazardTrackingModeTracked)){failureLog("FUXI MTL: heap reject descriptor type=%lu size=%lu storage=%lu cache=%lu hazard=%lu\n",(unsigned long)d.type,(unsigned long)d.size,(unsigned long)d.storageMode,(unsigned long)d.cpuCacheMode,(unsigned long)d.hazardTrackingMode);return nil;}
 FuxiHeap *heap=[FuxiHeap new];heap->_device=[self retain];heap->_size=d.size;heap->_storage=d.storageMode;heap->_cacheMode=d.cpuCacheMode;return heap;
}
- (id)newEvent {CENSUS();FuxiEvent *e=[FuxiEvent new];e->_device=[self retain];return e;}
- (id)newSharedEvent {CENSUS();FuxiEvent *e=[FuxiSharedEvent new];e->_device=[self retain];e->_shared=YES;return e;}
- (id)newFence {CENSUS();FuxiFence *f=[FuxiFence new];f->_device=[self retain];return f;}
- (id)newResourceGroupFromResources:(const id __unsafe_unretained [])resources count:(NSUInteger)count {CENSUS();
 if(count>256 || (count && !resources)){unsupported(self,_cmd);return nil;}
 NSMutableArray *group=[NSMutableArray new];for(NSUInteger i=0;i<count;++i)if(resources[i]){
  if(![resources[i] isKindOfClass:[FuxiResource class]] || [resources[i] device]!=self){[group release];unsupported(self,_cmd);return nil;}
  [group addObject:resources[i]];
 }
 return group;
}
- (id)newBufferWithLength:(NSUInteger)n options:(MTLResourceOptions)options {CENSUS();FUXI_PHASE(newBuffer);
 // CPU mapping is internal for private resources; public contents remains nil.
 if(clientMode==fuxi::ClientMode::census)return nil;
 auto storage=(options&MTLResourceStorageModeMask)>>MTLResourceStorageModeShift;
 auto cache=(options&MTLResourceCPUCacheModeMask)>>MTLResourceCPUCacheModeShift;
 auto hazard=(options&MTLResourceHazardTrackingModeMask)>>MTLResourceHazardTrackingModeShift;
 if((options&~(MTLResourceStorageModeMask|MTLResourceCPUCacheModeMask|MTLResourceHazardTrackingModeMask)) ||
    (storage!=MTLStorageModeShared && storage!=MTLStorageModePrivate) || cache>MTLCPUCacheModeWriteCombined ||
    (hazard!=MTLHazardTrackingModeDefault && hazard!=MTLHazardTrackingModeTracked)){failureLog("FUXI MTL: buffer reject options %lu length=%lu\n",(unsigned long)options,(unsigned long)n);return nil;}
 FuxiBuffer *b=[FuxiBuffer new];b->_device=[self retain];b->_storage=(MTLStorageMode)storage;b->_cacheMode=(MTLCPUCacheMode)cache;
 try {b->_buffer=owner(self)->backend->allocate(n);}
 catch(const std::exception &e) {failureLog("FUXI MTL: buffer FAIL %s\n",e.what());[b release];return nil;}
 return b;
}
- (id)newBufferWithBytes:(const void *)bytes length:(NSUInteger)n options:(MTLResourceOptions)o {CENSUS();FUXI_PHASE(newBuffer);
 if(!bytes) {unsupported(self,_cmd);return nil;}
 FuxiBuffer *b=[self newBufferWithLength:n options:o];if(b)memcpy(b->_buffer->contents,bytes,n);return b;
}
- (id)newBufferWithBytesNoCopy:(void *)pointer length:(NSUInteger)length options:(MTLResourceOptions)options deallocator:(void (^)(void *,NSUInteger))deallocator {CENSUS();FUXI_PHASE(newBuffer);
 if(options!=MTLResourceStorageModeShared || !pointer || clientMode==fuxi::ClientMode::census){unsupported(self,_cmd);return nil;}
 FuxiBuffer *b=[FuxiBuffer new];b->_device=[self retain];
 try {b->_buffer=owner(self)->backend->importBuffer(pointer,length);}
 catch(const std::exception &e){
  // QuartzCore wraps CGImage bytes, which are often unaligned or read-only
  // copy-on-write pages the kernel will not wire for GPU access (M9 CA smoke
  // stage 3). Shadow them: contents stays the client's pointer and every
  // commit refreshes the GPU copy. GPU writes do not reach the client's pages.
  // Page-aligned or enclosing-page imports are tried first; log each refusal reason (bounded) with the running outcome counters.
  static std::atomic<unsigned> refusals{0};unsigned n=refusals.fetch_add(1);
  if(n<12 || !(n&511))fprintf(stderr,"FUXI MTL: no-copy buffer shadowed (%s) len=%lu import attempts=%llu aligned=%llu enclosed=%llu plan_refused=%llu vulkan_refused=%llu\n",e.what(),(unsigned long)length,(unsigned long long)fuxi::HostImportStats::attempts.load(),(unsigned long long)fuxi::HostImportStats::aligned.load(),(unsigned long long)fuxi::HostImportStats::enclosed.load(),(unsigned long long)fuxi::HostImportStats::refusedPlan.load(),(unsigned long long)fuxi::HostImportStats::refusedVulkan.load());
  try {b->_buffer=owner(self)->backend->allocate(length);}
  catch(const std::exception &a){failureLog("FUXI MTL: no-copy buffer FAIL %s\n",a.what());[b release];return nil;}
  memcpy(b->_buffer->contents,pointer,length);b->_shadow=YES;
 }
 b->_wrapped=pointer;b->_wrappedLength=length;b->_deallocator=[deallocator copy];return b;
}
- (id)newDepthStencilStateWithDescriptor:(MTLDepthStencilDescriptor *)d {CENSUS();FUXI_PHASE(newOther);
 auto neutral=[](MTLStencilDescriptor *s){return !s || fuxi::neutralStencil(s.stencilCompareFunction,s.stencilFailureOperation,s.depthFailureOperation,s.depthStencilPassOperation);};
 if(!d || !fuxi::neutralDepth(d.depthCompareFunction,d.depthWriteEnabled) || !neutral(d.frontFaceStencil) || !neutral(d.backFaceStencil)) {
  unsupported(self,_cmd);return nil;
 }
 FuxiDepthStencil *s=[FuxiDepthStencil new];s->_device=[self retain];s->_label=[d.label copy];return s;
}
- (id)newLibraryWithData:(dispatch_data_t)data error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);
 if(error)*error=nil;if(clientMode==fuxi::ClientMode::census)return nil;
 if(!data || !dispatch_data_get_size(data) || dispatch_data_get_size(data)>4*1024*1024)return metalError(error,"library outside 1..4 MiB");
 const void *bytes=nullptr;size_t size=0;dispatch_data_t mapped=dispatch_data_create_map(data,&bytes,&size);
 if(!mapped)return metalError(error,"map library failed");
 FuxiLibrary *lib=[FuxiLibrary new];lib->_device=[self retain];char why[2048]{};
 try {auto &tr=fuxi::Translator::get();fuxi::Translator::OptionsScope scope(tr);lib->_library=tr.open((const uint8_t *)bytes,size,why,sizeof(why));if(lib->_library)owner(self)->backend->registerLibrary(lib->_library,fuxi::libraryHash(bytes,size));}
 catch(const std::exception &e) {dispatch_release(mapped);[lib release];return metalError(error,e.what());}
 dispatch_release(mapped);if(!lib->_library) {[lib release];return metalError(error,why);}return lib;
}
- (id)newLibraryWithFile:(NSString *)path error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);return [self newLibraryWithURL:path?[NSURL fileURLWithPath:path]:nil error:error];}
- (id)newDefaultLibrary {CENSUS();FUXI_PHASE(newOther);return [self newDefaultLibraryWithBundle:[NSBundle mainBundle] error:nullptr];}
- (id)newDefaultLibraryWithBundle:(NSBundle *)bundle error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);
 return [self newLibraryWithURL:[bundle URLForResource:@"default" withExtension:@"metallib"] error:error];
}
// Pipeline libraries are an optional precompiled cache; callers fall back to
// building pipelines, so report absence without counting a failure.
- (id)newPipelineLibraryWithFilePath:(NSString *)path error:(NSError **)error {CENSUS();(void)path;
 if(error)*error=[NSError errorWithDomain:@"org.fuxi.metal" code:2 userInfo:@{NSLocalizedDescriptionKey:@"no pipeline library support"}];return nil;
}
- (id)newLibraryWithURL:(NSURL *)url error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);
 if(error)*error=nil;if(clientMode==fuxi::ClientMode::census)return nil;
 if(!url.isFileURL)return metalError(error,"library requires a file URL");
 int fd=open(url.fileSystemRepresentation,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);struct stat st{};
 if(fd<0)return metalError(error,"open library failed");
 if(fstat(fd,&st) || !S_ISREG(st.st_mode) || st.st_size<=0 || st.st_size>4*1024*1024) {::close(fd);return metalError(error,"library file type/length");}
 NSMutableData *bytes=[NSMutableData dataWithLength:(NSUInteger)st.st_size];size_t done=0;
 while(done<bytes.length) {ssize_t n=read(fd,(uint8_t *)bytes.mutableBytes+done,bytes.length-done);
  if(n<0 && errno==EINTR)continue;if(n<=0) {::close(fd);return metalError(error,"read library failed");}done+=(size_t)n;}
 uint8_t extra;ssize_t tail;do {tail=read(fd,&extra,1);}while(tail<0 && errno==EINTR);::close(fd);
 if(tail!=0)return metalError(error,"library changed length during read");
 dispatch_data_t data=dispatch_data_create(bytes.bytes,bytes.length,dispatch_get_global_queue(0,0),DISPATCH_DATA_DESTRUCTOR_DEFAULT);
 id result=[self newLibraryWithData:data error:error];dispatch_release(data);return result;
}
- (id)newTextureWithDescriptor:(MTLTextureDescriptor *)d {CENSUS();FUXI_PHASE(newTexture);
 if(clientMode==fuxi::ClientMode::census)return nil;
 if(!textureDescriptorAccepted(d,owner(self)->backend->maxTextureDimension()))return nil;
 FuxiTexture *t=[FuxiTexture new];t->_descriptorText=[textureDescriptorText(d) copy];t->_cacheMode=d.cpuCacheMode;t->_device=[self retain];t->_usage=d.usage;t->_shared=d.storageMode==MTLStorageModeShared;t->_memoryless=d.storageMode==MTLStorageModeMemoryless;
 try {t->_texture=owner(self)->backend->allocateTexture((uint32_t)d.pixelFormat,(uint32_t)d.usage,(uint32_t)d.width,(uint32_t)d.height,(uint32_t)d.mipmapLevelCount,(uint32_t)d.textureType,(uint32_t)d.depth);}catch(const std::exception &e) {failureLog("FUXI MTL: texture reject allocation 0 desc=%s reason=%s\n",textureDescriptorText(d).UTF8String,e.what());[t release];return nil;}return t;
}
- (id)newTextureWithBytesNoCopy:(void *)bytes length:(NSUInteger)length descriptor:(MTLTextureDescriptor *)d deallocator:(void (^)(void *,NSUInteger))deallocator {CENSUS();FUXI_PHASE(newTexture);
 (void)bytes;(void)deallocator;
 failureLog("FUXI MTL: texture reject bytesNoCopy 1 desc=%s length=%lu (use validated buffer/linear texture path)\n",textureDescriptorText(d).UTF8String,(unsigned long)length);return nil;
}
- (id)newTextureWithDescriptor:(MTLTextureDescriptor *)d iosurface:(IOSurfaceRef)surface plane:(NSUInteger)plane {CENSUS();FUXI_PHASE(iosurface);
 if(clientMode==fuxi::ClientMode::census)return nil;
 // Inspect the surface before the descriptor: even RGB8_444_2P (542) refusals
 // must include both planes' layout, rather than only an unknown Metal format.
 NSString *layout=surfaceDescriptorText(surface,plane);
#define CHECK(field,value,accepted) do {__typeof__(value) actual=(value);if(!(accepted)){failureLog("FUXI MTL: texture reject %s %llu desc=%s %s\n",field,(unsigned long long)actual,textureDescriptorText(d).UTF8String,layout.UTF8String);return nil;}}while(0)
 CHECK("IOSurface.present",surface,actual!=nullptr);
 size_t planes=IOSurfaceGetPlaneCount(surface);
 // The pinned bridge has no two-plane RGB rendering/sampling contract. Never
 // reinterpret _RB4 as BGRA or silently discard its red/blue plane.
 CHECK("IOSurface.planeCount",planes,actual<=1);
 CHECK("IOSurface.plane",plane,actual==0);
 CHECK("IOSurface.fourcc",IOSurfaceGetPixelFormat(surface),actual==0x42475241);
 if(!textureDescriptorAccepted(d,owner(self)->backend->maxTextureDimension()))return nil;
 size_t width=planes?IOSurfaceGetWidthOfPlane(surface,plane):IOSurfaceGetWidth(surface);
 size_t height=planes?IOSurfaceGetHeightOfPlane(surface,plane):IOSurfaceGetHeight(surface);
 size_t row=planes?IOSurfaceGetBytesPerRowOfPlane(surface,plane):IOSurfaceGetBytesPerRow(surface);
 CHECK("IOSurface.width",width,actual==d.width);CHECK("IOSurface.height",height,actual==d.height);
 CHECK("IOSurface.bytesPerElement",planes?IOSurfaceGetBytesPerElementOfPlane(surface,plane):IOSurfaceGetBytesPerElement(surface),actual==4);
 CHECK("IOSurface.pixelFormat",d.pixelFormat,actual==MTLPixelFormatBGRA8Unorm);
 CHECK("IOSurface.textureType",d.textureType,actual==MTLTextureType2D);CHECK("IOSurface.mipmapLevelCount",d.mipmapLevelCount,actual==1);CHECK("IOSurface.storageMode",d.storageMode,actual==MTLStorageModeShared);
 CHECK("IOSurface.hostImport",owner(self)->backend->supportsHostImport(),actual);
 CHECK("IOSurface.lock",IOSurfaceLock(surface,kIOSurfaceLockAvoidSync,nullptr),actual==0);
#undef CHECK
 FuxiTexture *t=[FuxiTexture new];t->_descriptorText=[textureDescriptorText(d) copy];t->_cacheMode=d.cpuCacheMode;t->_device=[self retain];t->_usage=d.usage;
 t->_surface=(IOSurfaceRef)CFRetain(surface);t->_surfaceLockHeld=YES;IOSurfaceIncrementUseCount(surface);
 try {
  void *base=IOSurfaceGetBaseAddress(surface);void *start=planes?IOSurfaceGetBaseAddressOfPlane(surface,plane):base;
  if(!base || !start || uintptr_t(start)<uintptr_t(base))throw std::runtime_error("IOSurface plane base address");
  size_t offset=uintptr_t(start)-uintptr_t(base);
  t->_texture=owner(self)->backend->importTexture(base,IOSurfaceGetAllocSize(surface),row,(uint32_t)width,(uint32_t)height,offset,IOSurfaceGetID(surface));
 }catch(const std::exception &e) {failureLog("FUXI MTL: texture reject IOSurfaceImport 0 desc=%s %s reason=%s\n",textureDescriptorText(d).UTF8String,layout.UTF8String,e.what());[t release];return nil;}
 if(gpuScanout){IOSurfaceUnlock(surface,0,nullptr);t->_surfaceLockHeld=NO;}
 fprintf(stderr,"FUXI MTL: IOSurface import PASS BGRA8 %lux%lu same-memory %s\n",(unsigned long)d.width,(unsigned long)d.height,layout.UTF8String);return t;
}
// Repeat, mirror repeat, clamp-to-zero and clamp-to-border map to core Vulkan; mirror clamp-to-edge stays refused.
static bool fuxiAddressMode(MTLSamplerAddressMode m) {return m==MTLSamplerAddressModeClampToEdge || m==MTLSamplerAddressModeRepeat ||
 m==MTLSamplerAddressModeMirrorRepeat || m==MTLSamplerAddressModeClampToZero || m==MTLSamplerAddressModeClampToBorderColor;}
- (id)newSamplerStateWithDescriptor:(MTLSamplerDescriptor *)d {CENSUS();FUXI_PHASE(newOther);
 if(clientMode==fuxi::ClientMode::census)return nil;
 if(!d || d.minFilter>MTLSamplerMinMagFilterLinear || d.magFilter>MTLSamplerMinMagFilterLinear ||
    d.mipFilter>MTLSamplerMipFilterLinear || !fuxiAddressMode(d.sAddressMode) ||
    !fuxiAddressMode(d.tAddressMode) || !fuxiAddressMode(d.rAddressMode) || d.borderColor>MTLSamplerBorderColorOpaqueWhite ||
    !d.normalizedCoordinates || d.maxAnisotropy<1 || d.maxAnisotropy>16 || d.compareFunction!=MTLCompareFunctionNever ||
    !std::isfinite(d.lodMinClamp) || !std::isfinite(d.lodMaxClamp) || d.lodMinClamp<0 || d.lodMaxClamp<d.lodMinClamp || d.lodAverage || d.supportArgumentBuffers) {failureLog("FUXI MTL: sampler reject descriptor min=%lu mag=%lu mip=%lu lod=%.9g..%.9g address=%lu,%lu,%lu normalized=%u anisotropy=%lu compare=%lu average=%u arguments=%u\n",(unsigned long)d.minFilter,(unsigned long)d.magFilter,(unsigned long)d.mipFilter,d.lodMinClamp,d.lodMaxClamp,(unsigned long)d.sAddressMode,(unsigned long)d.tAddressMode,(unsigned long)d.rAddressMode,d.normalizedCoordinates,(unsigned long)d.maxAnisotropy,(unsigned long)d.compareFunction,d.lodAverage,d.supportArgumentBuffers);return nil;}
 FuxiSampler *state=[FuxiSampler new];state->_device=[self retain];state->_label=[d.label copy];
 try {const uint32_t address[3]={(uint32_t)d.sAddressMode,(uint32_t)d.tAddressMode,(uint32_t)d.rAddressMode};
  state->_sampler=owner(self)->backend->allocateSampler(d.minFilter==MTLSamplerMinMagFilterLinear,d.magFilter==MTLSamplerMinMagFilterLinear,(uint32_t)d.mipFilter,d.lodMinClamp,d.lodMaxClamp,address,(uint32_t)d.borderColor,(uint32_t)d.maxAnisotropy);}
 catch(const std::exception &e) {failureLog("FUXI MTL: sampler FAIL %s\n",e.what());[state release];return nil;}return state;
}
- (id)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);
 if(error)*error=nil;if(clientMode==fuxi::ClientMode::census)return nil;
 DescriptorContext context(pipelineDescriptorText(d));
 if(NSString *field=pipelineRejection(d,self))return surveyPipeline(metalError(error,field.UTF8String),error,self);
 id v=d.vertexFunction,f=d.fragmentFunction;
 FuxiPipeline *p=[FuxiPipeline new];p->_descriptorText=[pipelineDescriptorText(d) copy];p->_device=[self retain];p->_label=[d.label copy];
 try {auto a=(FuxiFunction *)v,b=(FuxiFunction *)f;fuxi::CompileOptions o{};auto color=d.colorAttachments[0];
  o.format=(uint32_t)color.pixelFormat;o.blend=color.blendingEnabled;o.mask=(uint32_t)color.writeMask;
  o.srcRGB=(uint32_t)color.sourceRGBBlendFactor;o.dstRGB=(uint32_t)color.destinationRGBBlendFactor;o.rgbOp=(uint32_t)color.rgbBlendOperation;
  o.srcAlpha=(uint32_t)color.sourceAlphaBlendFactor;o.dstAlpha=(uint32_t)color.destinationAlphaBlendFactor;o.alphaOp=(uint32_t)color.alphaBlendOperation;
  for(unsigned i=1;i<8;++i){auto c=d.colorAttachments[i];if(c.pixelFormat!=MTLPixelFormatInvalid)o.colorCount=i+1;
   o.colors[i-1]={(uint32_t)c.pixelFormat,(uint32_t)c.blendingEnabled,(uint32_t)c.sourceRGBBlendFactor,(uint32_t)c.destinationRGBBlendFactor,(uint32_t)c.rgbBlendOperation,(uint32_t)c.sourceAlphaBlendFactor,(uint32_t)c.destinationAlphaBlendFactor,(uint32_t)c.alphaBlendOperation,(uint32_t)c.writeMask};}
  for(unsigned i=0;i<8;++i){auto attr=d.vertexDescriptor.attributes[i];o.attributes[i]={(uint32_t)attr.format,(uint32_t)attr.offset,(uint32_t)attr.bufferIndex};}
  for(unsigned i=0;i<4;++i)o.strides[i]=(uint32_t)d.vertexDescriptor.layouts[i].stride;
  NSData *constants[2]={a->_constants,b->_constants};for(unsigned i=0;i<2;++i){o.constantCount[i]=(uint32_t)(constants[i].length/sizeof(fuxi::Constant));if(o.constantCount[i]>fuxi::maxFunctionConstants)throw std::runtime_error("constants bound");if(constants[i].length)memcpy(o.constants[i],constants[i].bytes,constants[i].length);}
  p->_pipeline=owner(self)->backend->compilePipeline(a->_library->_library,a->_name.UTF8String,b->_library->_library,b->_name.UTF8String,o);}
 catch(const std::exception &e) {[p release];return surveyPipeline(metalError(error,e.what()),error,self);}return p;
}
- (id)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d options:(MTLPipelineOption)options reflection:(MTLRenderPipelineReflection **)reflection error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);
 DescriptorContext context(pipelineDescriptorText(d));
 if(reflection)*reflection=nil;
 if(options!=MTLPipelineOptionNone)return metalError(error,"pipeline reflection/archive options deferred");
 return [self newRenderPipelineStateWithDescriptor:d error:error];
}
// The bounded frontend has no compute translator/encoder. Return the same
// explicit error through every synchronous and completion-handler entry point.
- (id)newComputePipelineStateWithFunction:(id<MTLFunction>)function error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);(void)function;
 return metalError(error,"compute pipelines deferred");
}
- (id)newComputePipelineStateWithFunction:(id<MTLFunction>)function options:(MTLPipelineOption)options reflection:(MTLComputePipelineReflection **)reflection error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);(void)options;
 if(reflection)*reflection=nil;return [self newComputePipelineStateWithFunction:function error:error];
}
- (id)newComputePipelineStateWithDescriptor:(MTLComputePipelineDescriptor *)d options:(MTLPipelineOption)options reflection:(MTLComputePipelineReflection **)reflection error:(NSError **)error {CENSUS();FUXI_PHASE(newOther);
 return [self newComputePipelineStateWithFunction:d.computeFunction options:options reflection:reflection error:error];
}
// Default remains inline for the existing RenderBox/CI compatibility path.
// Opt-in asynchronous render APIs snapshot input before returning.
// Balance the synchronous new-method ownership after the handler has seen it.
- (void)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d completionHandler:(MTLNewRenderPipelineStateCompletionHandler)handler {CENSUS();FUXI_PHASE(newOther);
 if(fuxi::systemTranslationPolicy().async) {
  MTLRenderPipelineDescriptor *snapshot=[d copy];
  dispatch_async(pipelineCompilationQueue(),^{@autoreleasepool {
   NSError *error=nil;id state=[self newRenderPipelineStateWithDescriptor:snapshot error:&error];
   @try {if(handler)handler((id<MTLRenderPipelineState>)state,error);}@finally{[state release];}
  }});[snapshot release];return;
 }
 NSError *error=nil;id state=[self newRenderPipelineStateWithDescriptor:d error:&error];
 @try {if(handler)handler((id<MTLRenderPipelineState>)state,error);}@finally{[state release];}
}
- (void)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d options:(MTLPipelineOption)options completionHandler:(MTLNewRenderPipelineStateWithReflectionCompletionHandler)handler {CENSUS();FUXI_PHASE(newOther);
 if(fuxi::systemTranslationPolicy().async) {
  MTLRenderPipelineDescriptor *snapshot=[d copy];
  dispatch_async(pipelineCompilationQueue(),^{@autoreleasepool {
   NSError *error=nil;MTLRenderPipelineReflection *reflection=nil;
   id state=[self newRenderPipelineStateWithDescriptor:snapshot options:options reflection:&reflection error:&error];
   @try {if(handler)handler((id<MTLRenderPipelineState>)state,reflection,error);}@finally{[state release];}
  }});[snapshot release];return;
 }
 NSError *error=nil;MTLRenderPipelineReflection *reflection=nil;id state=[self newRenderPipelineStateWithDescriptor:d options:options reflection:&reflection error:&error];
 @try {if(handler)handler((id<MTLRenderPipelineState>)state,reflection,error);}@finally{[state release];}
}
- (void)newComputePipelineStateWithFunction:(id<MTLFunction>)function completionHandler:(MTLNewComputePipelineStateCompletionHandler)handler {CENSUS();FUXI_PHASE(newOther);
 NSError *error=nil;id state=[self newComputePipelineStateWithFunction:function error:&error];
 @try {if(handler)handler((id<MTLComputePipelineState>)state,error);}@finally{[state release];}
}
- (void)newComputePipelineStateWithFunction:(id<MTLFunction>)function options:(MTLPipelineOption)options completionHandler:(MTLNewComputePipelineStateWithReflectionCompletionHandler)handler {CENSUS();FUXI_PHASE(newOther);
 NSError *error=nil;MTLComputePipelineReflection *reflection=nil;id state=[self newComputePipelineStateWithFunction:function options:options reflection:&reflection error:&error];
 @try {if(handler)handler((id<MTLComputePipelineState>)state,reflection,error);}@finally{[state release];}
}
- (void)newComputePipelineStateWithDescriptor:(MTLComputePipelineDescriptor *)d options:(MTLPipelineOption)options completionHandler:(MTLNewComputePipelineStateWithReflectionCompletionHandler)handler {CENSUS();FUXI_PHASE(newOther);
 NSError *error=nil;MTLComputePipelineReflection *reflection=nil;id state=[self newComputePipelineStateWithDescriptor:d options:options reflection:&reflection error:&error];
 @try {if(handler)handler((id<MTLComputePipelineState>)state,reflection,error);}@finally{[state release];}
}
// MTLAddDevice on 16.7 calls these hooks. Capabilities come exclusively from
// our overrides; no Apple family/limit table is installed in the base object.
- (void)initLimits {CENSUS();}
- (void)initFeatureQueries {CENSUS();}
- (id)_deviceWrapper {CENSUS();return self;}
@end
static id allocateDevice(Class cls,SEL selector,NSZone *zone) {(void)selector;(void)zone;return class_createInstance(cls,0);}
static void initializeDevice(Class cls,SEL selector) {(void)cls;(void)selector;}
static void copyMethods(Class from,Class to) {
 unsigned n=0;Method *methods=class_copyMethodList(from,&n);
 for(unsigned i=0;i<n;++i)class_replaceMethod(to,method_getName(methods[i]),method_getImplementation(methods[i]),method_getTypeEncoding(methods[i]));
 free(methods);
}
static void rejectProtocol(Class cls,Protocol *proto) {
 if(!proto)return;
 for(int required=0;required<2;++required) {
  unsigned n=0;auto *methods=protocol_copyMethodDescriptionList(proto,required,YES,&n);
  for(unsigned i=0;i<n;++i) if(!class_getInstanceMethod(cls,methods[i].name))
   class_addMethod(cls,methods[i].name,(IMP)_objc_msgForward,methods[i].types);
  free(methods);
 }
 unsigned n=0;Protocol *__unsafe_unretained *parents=protocol_copyProtocolList(proto,&n);
 for(unsigned i=0;i<n;++i)rejectProtocol(cls,parents[i]);free(parents);
}
// A constructor runs during NSBundle load, before principalClass lookup.
__attribute__((unused)) static Class makeDeviceClass(Class base) {
 if(!base || class_getInstanceSize(base)!=528) {
  failureLog("FUXI MTL: _MTLDevice size guard FAIL got %zu expected 528\n",base?class_getInstanceSize(base):0);return Nil;
 }
 Class cls=objc_allocateClassPair(base,"FuxiMTLDevice",0);if(!cls)return Nil;
 // Override inherited private implementations: unimplemented selectors must
 // reach forwarding, never execute against uninitialized private base ivars.
 for(Class c=base;c && c!=[NSObject class];c=class_getSuperclass(c)) {
  unsigned n=0;Method *m=class_copyMethodList(c,&n);
  for(unsigned i=0;i<n;++i)if(strncmp(sel_getName(method_getName(m[i])),".cxx_",5)) class_addMethod(cls,method_getName(m[i]),(IMP)_objc_msgForward,method_getTypeEncoding(m[i]));
  free(m);
 }
 copyMethods([NSObject class],cls);copyMethods([FuxiObject class],cls);copyMethods([FuxiDeviceMethods class],cls);
 for(const char *p:{"MTLDevice","MTLDeviceSPI"}) {Protocol *proto=objc_getProtocol(p);if(proto) {rejectProtocol(cls,proto);class_addProtocol(cls,proto);}}
 class_replaceMethod(object_getClass(cls),@selector(allocWithZone:),(IMP)allocateDevice,"@@:^v");
 class_replaceMethod(object_getClass(cls),@selector(initialize),(IMP)initializeDevice,"v@:");
 objc_registerClassPair(cls);
 fprintf(stderr,"FUXI MTL: registered FuxiMTLDevice base-size %zu instance-size %zu\n",class_getInstanceSize(base),class_getInstanceSize(cls));
 return cls;
}
#ifndef FUXI_MTL_FRONTEND_TEST
__attribute__((constructor)) static void registerDevice() {makeDeviceClass(objc_getClass("_MTLDevice"));}
#endif
// Public protocol selectors on resource classes also forward with real signatures.
__attribute__((constructor)) static void registerResources() {
 for(auto entry: {std::pair<Class,const char *>{[FuxiBuffer class],"MTLBuffer"},
      {[FuxiHeap class],"MTLHeap"},{[FuxiEvent class],"MTLEvent"},{[FuxiSharedEvent class],"MTLSharedEvent"},{[FuxiFence class],"MTLFence"},{[FuxiDepthStencil class],"MTLDepthStencilState"},
      {[FuxiQueue class],"MTLCommandQueue"},{[FuxiCommandBuffer class],"MTLCommandBuffer"},{[FuxiBlit class],"MTLBlitCommandEncoder"},{[FuxiRender class],"MTLRenderCommandEncoder"},
      {[FuxiSampler class],"MTLSamplerState"},{[FuxiTexture class],"MTLTexture"},{[FuxiLibrary class],"MTLLibrary"},{[FuxiFunction class],"MTLFunction"},{[FuxiPipeline class],"MTLRenderPipelineState"}}) {
  Protocol *p=objc_getProtocol(entry.second);rejectProtocol(entry.first,p);if(p)class_addProtocol(entry.first,p);
 }
}
