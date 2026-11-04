/* Native Mac ownership/descriptor/encoder tests; CPU model, GPU tested by M9k. */
#define FUXI_MTL_HOST_TEST
#define FUXI_MTL_FRONTEND_TEST
#define FUXI_MTL_BACKEND_HEADER "TestBackend.hpp"
#include "Frontend.mm"
#include "IOS16Defaults.hpp"
#include <cassert>
@interface FuxiMipTestBase : NSObject {uint8_t bytes[520];}
@end
@implementation FuxiMipTestBase
@end
int main(){@autoreleasepool {
 Class cls=makeDeviceClass([FuxiMipTestBase class]);id device=[cls alloc];FuxiOwner *state=[FuxiOwner new];state->backend=new fuxi::Backend;
 objc_setAssociatedObject(device,&ownerKey,state,OBJC_ASSOCIATION_RETAIN_NONATOMIC);[state release];fuxi::Backend::modelTextures=true;
 FuxiQueue *q=(FuxiQueue *)[device newCommandQueue];
 MTLTextureDescriptor *d=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:13 height:7 mipmapped:YES];
 d.storageMode=MTLStorageModeShared;d.cpuCacheMode=MTLCPUCacheModeWriteCombined;d.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget|MTLTextureUsagePixelFormatView;
 FuxiTexture *t=(FuxiTexture *)[device newTextureWithDescriptor:d];assert(t && t.mipmapLevelCount==4 && t.cpuCacheMode==MTLCPUCacheModeWriteCombined);
 uint8_t input[512],output[512];
 for(unsigned l=0;l<4;++l){unsigned w=std::max(1u,13u>>l),h=std::max(1u,7u>>l),row=w*4+8;for(unsigned i=0;i<sizeof(input);++i)input[i]=(uint8_t)(i+43*l);memset(output,0xa5,sizeof(output));
  [t replaceRegion:MTLRegionMake2D(0,0,w,h) mipmapLevel:l withBytes:input bytesPerRow:row];
  [t getBytes:output bytesPerRow:row fromRegion:MTLRegionMake2D(0,0,w,h) mipmapLevel:l];
  for(unsigned y=0;y<h;++y){assert(!memcmp(output+y*row,input+y*row,w*4));for(unsigned x=w*4;x<row;++x)assert(output[y*row+x]==0xa5);}
 }
 auto errors=failureCount.load();
 FuxiTexture *view=(FuxiTexture *)[t newTextureViewWithPixelFormat:t.pixelFormat textureType:MTLTextureType2D levels:NSMakeRange(1,3) slices:NSMakeRange(0,1)];
 FuxiTexture *nested=(FuxiTexture *)[view newTextureViewWithPixelFormat:t.pixelFormat textureType:MTLTextureType2D levels:NSMakeRange(1,1) slices:NSMakeRange(0,1)];
 assert(view && view.width==6 && view.height==3 && view.mipmapLevelCount==3 && view.parentRelativeLevel==1);
 assert(nested.width==3 && nested.height==1 && nested.parentRelativeLevel==1 && nested->_baseLevel==2);
 uint8_t patch[4]={7,31,97,255};[nested replaceRegion:MTLRegionMake2D(1,0,1,1) mipmapLevel:0 withBytes:patch bytesPerRow:4];
 [t getBytes:output bytesPerRow:12 fromRegion:MTLRegionMake2D(0,0,3,1) mipmapLevel:2];assert(!memcmp(output+4,patch,4));
 FuxiCommandBuffer *c=(FuxiCommandBuffer *)[q commandBuffer];FuxiBlit *blit=(FuxiBlit *)[c blitCommandEncoder];[blit generateMipmapsForTexture:(id)view];
 assert(c->_ops.size()==1 && c->_ops[0].generateMips && c->_ops[0].mipBase==1 && c->_ops[0].mipCount==3);[blit endEncoding]; // Model deliberately has no GPU filtering.
 d.cpuCacheMode=MTLCPUCacheModeDefaultCache;FuxiTexture *copy=(FuxiTexture *)[device newTextureWithDescriptor:d];
 c=(FuxiCommandBuffer *)[q commandBuffer];blit=(FuxiBlit *)[c blitCommandEncoder];[blit copyFromTexture:(id)t toTexture:(id)copy];[blit endEncoding];assert(c->_ops.size()==4 && c->_ops[3].srcLevel==3 && c->_ops[3].dstLevel==3);[c commit];assert(!c.error);
 [copy getBytes:output bytesPerRow:12 fromRegion:MTLRegionMake2D(0,0,3,1) mipmapLevel:2];assert(!memcmp(output+4,patch,4));
 // All three blit directions translate view-relative levels and retain owners.
 FuxiBuffer *buffer=(FuxiBuffer *)[device newBufferWithLength:64 options:0];memcpy(buffer.contents,patch,4);
 c=(FuxiCommandBuffer *)[q commandBuffer];blit=(FuxiBlit *)[c blitCommandEncoder];
 [blit copyFromBuffer:buffer sourceOffset:0 sourceBytesPerRow:4 sourceBytesPerImage:0 sourceSize:MTLSizeMake(1,1,1) toTexture:nested destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0,0,0)];
 [blit copyFromTexture:nested sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(1,1,1) toBuffer:buffer destinationOffset:16 destinationBytesPerRow:4 destinationBytesPerImage:0];
 assert(c->_ops[0].dstLevel==2 && c->_ops[1].srcLevel==2);[blit endEncoding];[c commit];assert(!c.error && !memcmp((uint8_t *)buffer.contents+16,patch,4));
 // A level-one attachment on a rebased view targets absolute level two.
 MTLRenderPassDescriptor *pass=ios16PassDefaults();pass.colorAttachments[0].texture=(id)view;pass.colorAttachments[0].level=1;
 pass.colorAttachments[0].loadAction=MTLLoadActionClear;pass.colorAttachments[0].storeAction=MTLStoreActionStore;pass.colorAttachments[0].clearColor=MTLClearColorMake(1,0,0,1);
 c=(FuxiCommandBuffer *)[q commandBuffer];FuxiRender *r=(FuxiRender *)[c renderCommandEncoderWithDescriptor:pass];assert(r && r->_state.dstLevel==2 && r->_state.areaWidth==3 && r->_state.areaHeight==1);[r endEncoding];[c commit];assert(!c.error);
 [t getBytes:output bytesPerRow:12 fromRegion:MTLRegionMake2D(0,0,3,1) mipmapLevel:2];for(unsigned x=0;x<3;++x){uint8_t red[]={0,0,255,255};assert(!memcmp(output+4*x,red,4));}
 // The supplied Spotlight shape binds its full mip view before draw-time
 // access analysis. Object identity alone must not fail this encoder.
 auto blurDescriptor=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:704 height:96 mipmapped:YES];blurDescriptor.storageMode=MTLStorageModePrivate;blurDescriptor.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget;
 FuxiTexture *blur=(FuxiTexture *)[device newTextureWithDescriptor:blurDescriptor];assert(blur.mipmapLevelCount==10);
 FuxiPipeline *modelPipeline=[FuxiPipeline new];modelPipeline->_device=[device retain];modelPipeline->_pipeline=new fuxi::Pipeline{};
 auto blurPass=ios16PassDefaults();blurPass.colorAttachments[0].texture=(id)blur;blurPass.colorAttachments[0].level=1;blurPass.colorAttachments[0].loadAction=MTLLoadActionClear;blurPass.colorAttachments[0].storeAction=MTLStoreActionStore;
 c=(FuxiCommandBuffer *)[q commandBuffer];r=(FuxiRender *)[c renderCommandEncoderWithDescriptor:blurPass];assert(r && r->_state.areaWidth==352 && r->_state.areaHeight==48);
 [r setFragmentTexture:blur atIndex:0];assert(!c->_rejected);[r setRenderPipelineState:modelPipeline];[r drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];[r endEncoding];
 assert(!c->_rejected && c->_ops.size()==1 && c->_ops[0].dstLevel==1 && c->_ops[0].samples[0]==blur->_texture && c->_ops[0].sampleBase[0]==0 && c->_ops[0].sampleCount[0]==10);[blur release];
 // Resource classes: public query values, private heap budgets, volatile/empty.
 for(MTLPixelFormat format:{MTLPixelFormatBGRA8Unorm,MTLPixelFormatRGBA8Unorm,MTLPixelFormatRGBA16Float,MTLPixelFormatR8Unorm,MTLPixelFormatRG8Unorm,MTLPixelFormatA8Unorm,MTLPixelFormatBGR10A2Unorm}){
  d.pixelFormat=format;d.usage=format==MTLPixelFormatA8Unorm?MTLTextureUsageShaderRead:MTLTextureUsageUnknown;d.storageMode=MTLStorageModePrivate;
  FuxiTexture *resource=(FuxiTexture *)[device newTextureWithDescriptor:d];assert(resource && resource.usage==d.usage && resource.storageMode==MTLStorageModePrivate && resource.mipmapLevelCount==4);[resource release];
 }
 d.pixelFormat=MTLPixelFormatBGRA8Unorm;d.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget;
 MTLHeapDescriptor *hd=[MTLHeapDescriptor new];hd.storageMode=MTLStorageModePrivate;hd.size=4096;
 FuxiHeap *heap=(FuxiHeap *)[device newHeapWithDescriptor:hd];FuxiTexture *ht=(FuxiTexture *)[heap newTextureWithDescriptor:d];
 id<MTLBuffer> hb=[heap newBufferWithLength:64 options:MTLResourceStorageModePrivate];assert(heap && ht && hb && !hb.contents && hb.storageMode==MTLStorageModePrivate);
 assert(ht.heap==heap && heap.usedSize==ht.allocatedSize+hb.allocatedSize);[ht release];[hb release];assert(!heap.usedSize);[heap release];[hd release];
 for(MTLSamplerMipFilter filter:{MTLSamplerMipFilterNotMipmapped,MTLSamplerMipFilterNearest,MTLSamplerMipFilterLinear}){MTLSamplerDescriptor *sd=[MTLSamplerDescriptor new];sd.mipFilter=filter;sd.lodMinClamp=1;sd.lodMaxClamp=2.5f;id sampler=[device newSamplerStateWithDescriptor:sd];assert(sampler);[sampler release];[sd release];}
 assert(failureCount==errors);
 [t setPurgeableState:MTLPurgeableStateEmpty];assert(!t->_texture->isDefined(0) && !t->_texture->isDefined(2));
 // Refusals use valid SDK values; do not provoke SDK descriptor-array assertions.
 d.storageMode=MTLStorageModeMemoryless;{FuxiTexture *m=(FuxiTexture *)[device newTextureWithDescriptor:d];assert(m && m.storageMode==MTLStorageModeMemoryless && m.mipmapLevelCount==d.mipmapLevelCount);[m release];}d.storageMode=MTLStorageModeShared;
 d.textureType=MTLTextureType2DArray;d.arrayLength=2;assert(![device newTextureWithDescriptor:d]);d.textureType=MTLTextureType2D;d.arrayLength=1;
 d.pixelFormat=MTLPixelFormatBGRA8Unorm_sRGB;assert(![device newTextureWithDescriptor:d]);d.pixelFormat=MTLPixelFormatDepth32Float;assert(![device newTextureWithDescriptor:d]);d.pixelFormat=MTLPixelFormatBGRA8Unorm;
 d.swizzle=MTLTextureSwizzleChannelsMake(MTLTextureSwizzleBlue,MTLTextureSwizzleGreen,MTLTextureSwizzleRed,MTLTextureSwizzleAlpha);assert(![device newTextureWithDescriptor:d]);d.swizzle=MTLTextureSwizzleChannelsMake(MTLTextureSwizzleRed,MTLTextureSwizzleGreen,MTLTextureSwizzleBlue,MTLTextureSwizzleAlpha);
 d.usage=MTLTextureUsageShaderWrite;assert(![device newTextureWithDescriptor:d]);d.usage=MTLTextureUsageShaderRead;
 // Nonplanar plane 1 is invalid; one explicit BGRA plane uses its own offset
 // and row stride while the entire surface allocation crosses HostImport.
 d.width=8;d.height=8;d.mipmapLevelCount=1;
 fuxi::Backend::modelHostImport=true;
 d.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget;
 IOSurfaceRef surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@8,(id)kIOSurfaceHeight:@8,(id)kIOSurfaceBytesPerElement:@4,(id)kIOSurfacePixelFormat:@0x42475241});
 assert(surface && ![device newTextureWithDescriptor:d iosurface:surface plane:1]);
 FuxiTexture *st=(FuxiTexture *)[device newTextureWithDescriptor:d iosurface:surface plane:0];
 assert(st && fuxi::Backend::importOffset==0 && fuxi::Backend::importRow==IOSurfaceGetBytesPerRow(surface));
 FuxiTexture *surfaceAlias=(FuxiTexture *)[device newTextureWithDescriptor:d iosurface:surface plane:0];assert(surfaceAlias && surfaceAlias!=st);
 auto surfacePass=ios16PassDefaults();surfacePass.colorAttachments[0].texture=(id)st;surfacePass.colorAttachments[0].loadAction=MTLLoadActionClear;surfacePass.colorAttachments[0].storeAction=MTLStoreActionStore;
 c=(FuxiCommandBuffer *)[q commandBuffer];r=(FuxiRender *)[c renderCommandEncoderWithDescriptor:surfacePass];[r setRenderPipelineState:modelPipeline];[r setFragmentTexture:surfaceAlias atIndex:0];assert(!c->_rejected);
 auto beforeSkip=fuxi::skippedSamplingDraws.load();[r drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
 assert(bool(c->_rejected)==!fuxi::systemTranslationPolicy().skipSampling);
 if(fuxi::systemTranslationPolicy().skipSampling){assert(c->_ops.empty() && fuxi::skippedSamplingDraws==beforeSkip+1);[r endEncoding];assert(c->_ops.size()==1 && c->_ops[0].clearOnly);}
 [surfaceAlias release];[st release];CFRelease(surface);
 surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@8,(id)kIOSurfaceHeight:@8,(id)kIOSurfacePixelFormat:@0x42475241,(id)kIOSurfaceAllocSize:@16384,
  (id)kIOSurfacePlaneInfo:@[@{(id)kIOSurfacePlaneWidth:@8,(id)kIOSurfacePlaneHeight:@8,(id)kIOSurfacePlaneBytesPerElement:@4,
   (id)kIOSurfacePlaneBytesPerRow:@128,(id)kIOSurfacePlaneOffset:@4096,(id)kIOSurfacePlaneSize:@1024}]});
 assert(surface && IOSurfaceGetPlaneCount(surface)==1);
 st=(FuxiTexture *)[device newTextureWithDescriptor:d iosurface:surface plane:0];
 assert(st && st.iosurface==surface && st.iosurfacePlane==0);
 assert(fuxi::Backend::importBase==IOSurfaceGetBaseAddress(surface) && fuxi::Backend::importBytes==IOSurfaceGetAllocSize(surface));
 assert(fuxi::Backend::importOffset==4096 && fuxi::Backend::importRow==128);[st release];
 assert(![device newTextureWithDescriptor:d iosurface:surface plane:1]);CFRelease(surface);
 surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@8,(id)kIOSurfaceHeight:@8,(id)kIOSurfacePixelFormat:@0x5f524234,(id)kIOSurfaceAllocSize:@16384,
  (id)kIOSurfacePlaneInfo:@[@{(id)kIOSurfacePlaneWidth:@8,(id)kIOSurfacePlaneHeight:@8,(id)kIOSurfacePlaneBytesPerElement:@1,(id)kIOSurfacePlaneBytesPerRow:@64,(id)kIOSurfacePlaneOffset:@0,(id)kIOSurfacePlaneSize:@512},
   @{(id)kIOSurfacePlaneWidth:@8,(id)kIOSurfacePlaneHeight:@8,(id)kIOSurfacePlaneBytesPerElement:@2,(id)kIOSurfacePlaneBytesPerRow:@64,(id)kIOSurfacePlaneOffset:@4096,(id)kIOSurfacePlaneSize:@512}]});
 assert(surface && IOSurfaceGetPlaneCount(surface)==2);d.pixelFormat=(MTLPixelFormat)542;
 auto hardFailures=failureCount.load(),soft=softRefusalCount.load();
 assert(![device newTextureWithDescriptor:d iosurface:surface plane:0]);
 assert(![device newTextureWithDescriptor:d iosurface:surface plane:1]);CFRelease(surface);
 // An IOSurface texture refusal fails that texture only: no failure count (backboardd's gate would not latch), one soft count each.
 assert(failureCount==hardFailures && softRefusalCount==soft+2);
 // h-daily2's 420v H.264 frame (mediaserverd, 332x198): CA wraps plane 0 as R8 at the plane's size and plane 1 as RG8 at half size,
 // sampled only. Each imports the whole allocation at its own offset and row.
 surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@332,(id)kIOSurfaceHeight:@198,(id)kIOSurfacePixelFormat:@0x34323076,(id)kIOSurfaceAllocSize:@131072,
  (id)kIOSurfacePlaneInfo:@[@{(id)kIOSurfacePlaneWidth:@332,(id)kIOSurfacePlaneHeight:@198,(id)kIOSurfacePlaneBytesPerElement:@1,(id)kIOSurfacePlaneBytesPerRow:@384,(id)kIOSurfacePlaneOffset:@64,(id)kIOSurfacePlaneSize:@79872},
   @{(id)kIOSurfacePlaneWidth:@166,(id)kIOSurfacePlaneHeight:@99,(id)kIOSurfacePlaneBytesPerElement:@2,(id)kIOSurfacePlaneBytesPerRow:@384,(id)kIOSurfacePlaneOffset:@79936,(id)kIOSurfacePlaneSize:@39936}]});
 assert(surface && IOSurfaceGetPlaneCount(surface)==2 && IOSurfaceGetBytesPerRowOfPlane(surface,1)==384);
 MTLTextureDescriptor *luma=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR8Unorm width:332 height:198 mipmapped:NO];
 luma.usage=MTLTextureUsageShaderRead;luma.storageMode=MTLStorageModeShared;
 MTLTextureDescriptor *chroma=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRG8Unorm width:166 height:99 mipmapped:NO];
 chroma.usage=MTLTextureUsageShaderRead;chroma.storageMode=MTLStorageModeShared;
 hardFailures=failureCount.load();soft=softRefusalCount.load();
 FuxiTexture *y=(FuxiTexture *)[device newTextureWithDescriptor:luma iosurface:surface plane:0];
 assert(y && y.iosurface==surface && y.iosurfacePlane==0 && y.pixelFormat==MTLPixelFormatR8Unorm && y.width==332 && y.height==198);
 assert(fuxi::Backend::importBase==IOSurfaceGetBaseAddress(surface) && fuxi::Backend::importBytes==131072 && fuxi::Backend::importOffset==64 && fuxi::Backend::importRow==384);
 assert(fuxi::Backend::importFormat==MTLPixelFormatR8Unorm && fuxi::Backend::importSampledOnly && !y->_texture->shadow);
 FuxiTexture *cbcr=(FuxiTexture *)[device newTextureWithDescriptor:chroma iosurface:surface plane:1];
 assert(cbcr && cbcr.iosurfacePlane==1 && cbcr.pixelFormat==MTLPixelFormatRG8Unorm && fuxi::Backend::importOffset==79936 && fuxi::Backend::importRow==384 && fuxi::Backend::importFormat==MTLPixelFormatRG8Unorm);
 FuxiTexture *lumaView=(FuxiTexture *)[y newTextureViewWithPixelFormat:MTLPixelFormatR8Unorm];assert(lumaView && lumaView.iosurfacePlane==0);[lumaView release];
 // Refused, softly: a plane as BGRA8 (never reinterpreted), a plane the surface lacks, a size or element size not the plane's.
 MTLTextureDescriptor *wrong=[luma copy];wrong.pixelFormat=MTLPixelFormatBGRA8Unorm;assert(![device newTextureWithDescriptor:wrong iosurface:surface plane:0]);
 assert(![device newTextureWithDescriptor:luma iosurface:surface plane:2]);
 assert(![device newTextureWithDescriptor:luma iosurface:surface plane:1]);
 wrong.pixelFormat=MTLPixelFormatRG8Unorm;assert(![device newTextureWithDescriptor:wrong iosurface:surface plane:0]);[wrong release];
 assert(failureCount==hardFailures && softRefusalCount==soft+4);
 // A rendered plane is not sampled-only: the backend may not shadow it.
 luma.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget;FuxiTexture *drawn=(FuxiTexture *)[device newTextureWithDescriptor:luma iosurface:surface plane:0];
 assert(drawn && !fuxi::Backend::importSampledOnly);[drawn release];luma.usage=MTLTextureUsageShaderRead;
 // A draw whose shader declares a texture slot left empty (CA got nil for a refused texture) is left out, not the submit.
 {FuxiPipeline *yuv=[FuxiPipeline new];yuv->_device=[device retain];yuv->_pipeline=new fuxi::Pipeline{};yuv->_pipeline->textureSlots=0x3;
  auto target=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:8 height:8 mipmapped:NO];target.usage=MTLTextureUsageRenderTarget|MTLTextureUsageShaderRead;target.storageMode=MTLStorageModePrivate;
  FuxiTexture *rt=(FuxiTexture *)[device newTextureWithDescriptor:target];assert(rt);auto yuvPass=ios16PassDefaults();yuvPass.colorAttachments[0].texture=(id)rt;
  yuvPass.colorAttachments[0].loadAction=MTLLoadActionClear;yuvPass.colorAttachments[0].storeAction=MTLStoreActionStore;
  auto dropped=fuxi::unboundTextureDraws.load();
  c=(FuxiCommandBuffer *)[q commandBuffer];r=(FuxiRender *)[c renderCommandEncoderWithDescriptor:yuvPass];[r setRenderPipelineState:yuv];
  [r setFragmentTexture:y atIndex:0];[r drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
  assert(!c->_rejected && c->_ops.empty() && fuxi::unboundTextureDraws==dropped+1);
  [r setFragmentTexture:cbcr atIndex:1];[r drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
  assert(!c->_rejected && c->_ops.size()==1 && c->_ops[0].samples[0]==y->_texture && c->_ops[0].samples[1]==cbcr->_texture && fuxi::unboundTextureDraws==dropped+1);
  [r endEncoding];assert(failureCount==hardFailures);[rt release];[yuv release];}
 [y release];[cbcr release];CFRelease(surface);
 // SpringBoard's 420f snapshot (788x1706, 800-byte rows, plane 1 at 1364800): rows no linear image aliases, so the sampled planes go
 // through the backend's shadow (modeled: marked shadow).
 surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@788,(id)kIOSurfaceHeight:@1706,(id)kIOSurfacePixelFormat:@0x34323066,(id)kIOSurfaceAllocSize:@2048000,
  (id)kIOSurfacePlaneInfo:@[@{(id)kIOSurfacePlaneWidth:@788,(id)kIOSurfacePlaneHeight:@1706,(id)kIOSurfacePlaneBytesPerElement:@1,(id)kIOSurfacePlaneBytesPerRow:@800,(id)kIOSurfacePlaneOffset:@0,(id)kIOSurfacePlaneSize:@1364800},
   @{(id)kIOSurfacePlaneWidth:@394,(id)kIOSurfacePlaneHeight:@853,(id)kIOSurfacePlaneBytesPerElement:@2,(id)kIOSurfacePlaneBytesPerRow:@800,(id)kIOSurfacePlaneOffset:@1364800,(id)kIOSurfacePlaneSize:@682400}]});
 assert(surface && IOSurfaceGetPlaneCount(surface)==2);
 luma.width=788;luma.height=1706;chroma.width=394;chroma.height=853;
 y=(FuxiTexture *)[device newTextureWithDescriptor:luma iosurface:surface plane:0];assert(y && y->_texture->shadow && fuxi::Backend::importRow==800 && fuxi::Backend::importOffset==0);
 cbcr=(FuxiTexture *)[device newTextureWithDescriptor:chroma iosurface:surface plane:1];assert(cbcr && cbcr->_texture->shadow && fuxi::Backend::importOffset==1364800);
 [y release];[cbcr release];CFRelease(surface);assert(failureCount==hardFailures);
 // v-yuv1 (YouTube in Safari): the host import refused (FuxiAdreno's import table full). A sampled plane or BGRA layer is copied by
 // the CPU (modeled: hostSource set); a render target cannot be, and is refused softly.
 fuxi::Backend::modelImportRefused=true;soft=softRefusalCount.load();
 surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@640,(id)kIOSurfaceHeight:@360,(id)kIOSurfacePixelFormat:@0x34323076,(id)kIOSurfaceAllocSize:@360448,
  (id)kIOSurfacePlaneInfo:@[@{(id)kIOSurfacePlaneWidth:@640,(id)kIOSurfacePlaneHeight:@360,(id)kIOSurfacePlaneBytesPerElement:@1,(id)kIOSurfacePlaneBytesPerRow:@640,(id)kIOSurfacePlaneOffset:@64,(id)kIOSurfacePlaneSize:@235520},
   @{(id)kIOSurfacePlaneWidth:@320,(id)kIOSurfacePlaneHeight:@180,(id)kIOSurfacePlaneBytesPerElement:@2,(id)kIOSurfacePlaneBytesPerRow:@640,(id)kIOSurfacePlaneOffset:@235584,(id)kIOSurfacePlaneSize:@115200}]});
 assert(surface);luma.width=640;luma.height=360;chroma.width=320;chroma.height=180;
 y=(FuxiTexture *)[device newTextureWithDescriptor:luma iosurface:surface plane:0];assert(y && y->_texture->hostSource==(uint8_t *)IOSurfaceGetBaseAddress(surface)+64 && y->_texture->shadow);
 cbcr=(FuxiTexture *)[device newTextureWithDescriptor:chroma iosurface:surface plane:1];assert(cbcr && cbcr->_texture->hostSource==(uint8_t *)IOSurfaceGetBaseAddress(surface)+235584);
 [y release];[cbcr release];CFRelease(surface);
 surface=IOSurfaceCreate((CFDictionaryRef)@{(id)kIOSurfaceWidth:@66,(id)kIOSurfaceHeight:@45,(id)kIOSurfaceBytesPerElement:@4,(id)kIOSurfaceBytesPerRow:@320,(id)kIOSurfacePixelFormat:@0x42475241});
 MTLTextureDescriptor *layer=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:66 height:45 mipmapped:NO];
 layer.usage=MTLTextureUsageShaderRead;layer.storageMode=MTLStorageModeShared;
 FuxiTexture *tile=(FuxiTexture *)[device newTextureWithDescriptor:layer iosurface:surface plane:0];
 assert(surface && tile && tile->_texture->hostSource==IOSurfaceGetBaseAddress(surface) && fuxi::Backend::importRow==IOSurfaceGetBytesPerRow(surface));[tile release];
 layer.usage=MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget;assert(![device newTextureWithDescriptor:layer iosurface:surface plane:0]);
 CFRelease(surface);fuxi::Backend::modelImportRefused=false;
 assert(failureCount==hardFailures && softRefusalCount==soft+1);
 [modelPipeline release];[buffer release];[copy release];[nested release];[t release];assert(view.width==6);[view release];[q release];[device release];
 puts("PASS: native M9k descriptor/level transfers/views/blits/generation encoding/render attachment/private heap/purge/sampler/refusal controls");
}}
