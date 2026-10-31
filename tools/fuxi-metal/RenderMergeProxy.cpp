/* Test-only Vulkan recording assertions, forwarding to the supplied local ICD. */
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <unordered_map>
#include <vector>
#include <array>
static PFN_vkGetInstanceProcAddr instanceProc;
static PFN_vkGetDeviceProcAddr deviceProc;
static PFN_vkCmdBeginRenderPass beginPass;
static PFN_vkCmdEndRenderPass endPass;
static PFN_vkCmdPipelineBarrier pipelineBarrier;
static PFN_vkCmdDraw draw;
static PFN_vkBeginCommandBuffer beginCommand;
static PFN_vkEndCommandBuffer endCommand;
static PFN_vkCreateRenderPass createPass;
static PFN_vkCreateGraphicsPipelines createPipelines;
static PFN_vkCreateFramebuffer createFramebuffer;
static PFN_vkCmdBindPipeline bindPipeline;
static PFN_vkUpdateDescriptorSets updateSets;
static PFN_vkCmdBindDescriptorSets bindSets;
static PFN_vkCmdCopyBufferToImage copyImage;
static PFN_vkGetPhysicalDeviceFormatProperties formatProperties;
static uint64_t formatQueries;
extern "C" uint64_t render_merge_format_queries(){return formatQueries;}
static PFN_vkEnumerateDeviceExtensionProperties enumerateExtensions;
static bool inside;
static uint64_t barrierCalls,imageBarrierCalls;
extern "C" uint64_t render_merge_barrier_calls(){return barrierCalls;}
extern "C" uint64_t render_merge_image_barrier_calls(){return imageBarrierCalls;}
static uint64_t passes,draws,copies,imageBarriers,submits,pipelineBinds,setBinds,viewportSets,scissorSets;
static PFN_vkCmdSetViewport setViewport;
static PFN_vkCmdSetScissor setScissor;
extern "C" uint64_t render_merge_pipeline_binds(){return pipelineBinds;}
extern "C" uint64_t render_merge_set_binds(){return setBinds;}
extern "C" uint64_t render_merge_viewport_sets(){return viewportSets;}
extern "C" uint64_t render_merge_scissor_sets(){return scissorSets;}
static void viewport(VkCommandBuffer c,uint32_t f,uint32_t n,const VkViewport *v){++viewportSets;setViewport(c,f,n,v);}
static void scissor(VkCommandBuffer c,uint32_t f,uint32_t n,const VkRect2D *v){++scissorSets;setScissor(c,f,n,v);}
static PFN_vkQueueSubmit queueSubmit;
extern "C" uint64_t render_merge_image_barriers(){return imageBarriers;}
extern "C" uint64_t render_merge_submits(){return submits;}
static VkResult submit(VkQueue q,uint32_t n,const VkSubmitInfo *s,VkFence f){++submits;return queueSubmit(q,n,s,f);}
// Fence fault injection for the split-submit tests: 1 = device lost, 2 = timeout (the GPU work itself still runs).
static PFN_vkWaitForFences waitFences;static int fenceFault;
extern "C" void render_merge_fence_fault(int mode){fenceFault=mode;}
static VkResult waited(VkDevice d,uint32_t n,const VkFence *f,VkBool32 all,uint64_t timeout){auto r=waitFences(d,n,f,all,timeout);return fenceFault==1?VK_ERROR_DEVICE_LOST:fenceFault==2?VK_TIMEOUT:r;}
static VkRenderPass activePass;
static VkFramebuffer activeFrame;
static VkDescriptorSet activeSet;
static std::unordered_map<VkRenderPass,std::pair<uint32_t,uint32_t>> actions;
static std::unordered_map<VkRenderPass,std::vector<uint32_t>> layouts;
static std::unordered_map<VkPipeline,VkRenderPass> pipelinePasses;
static std::unordered_map<VkFramebuffer,std::vector<VkImageView>> frameViews;
static std::unordered_map<VkDescriptorSet,std::vector<VkImageView>> inputViews;
static std::unordered_map<VkImage,std::array<VkImageLayout,32>> imageLayouts;
static uint32_t lastLoad,lastStore;
extern "C" uint64_t render_merge_passes(){return passes;}
extern "C" uint64_t render_merge_draws(){return draws;}
extern "C" uint64_t render_merge_load(){return lastLoad;}
extern "C" uint64_t render_merge_store(){return lastStore;}
extern "C" uint64_t render_merge_copies(){return copies;}
static VkResult create(VkDevice d,const VkRenderPassCreateInfo *i,const VkAllocationCallbacks *a,VkRenderPass *p){auto r=createPass(d,i,a,p);if(r==VK_SUCCESS && i->attachmentCount){actions[*p]={i->pAttachments[0].loadOp,i->pAttachments[0].storeOp};
 auto &key=layouts[*p];key.clear();for(unsigned n=0;n<i->attachmentCount;++n){key.push_back(i->pAttachments[n].format);key.push_back(i->pAttachments[n].samples);}
 assert(i->subpassCount==1);auto &s=i->pSubpasses[0];key.push_back(s.flags);key.push_back(s.colorAttachmentCount);key.push_back(s.inputAttachmentCount);
 for(unsigned n=0;n<s.colorAttachmentCount;++n){key.push_back(s.pColorAttachments[n].attachment);key.push_back(s.pColorAttachments[n].layout);}
 for(unsigned n=0;n<s.inputAttachmentCount;++n){key.push_back(s.pInputAttachments[n].attachment);key.push_back(s.pInputAttachments[n].layout);}
 }return r;}
static VkResult pipelines(VkDevice d,VkPipelineCache cache,uint32_t n,const VkGraphicsPipelineCreateInfo *i,const VkAllocationCallbacks *a,VkPipeline *p){auto r=createPipelines(d,cache,n,i,a,p);if(r==VK_SUCCESS)for(unsigned x=0;x<n;++x)pipelinePasses[p[x]]=i[x].renderPass;return r;}
static VkResult framebuffer(VkDevice d,const VkFramebufferCreateInfo *i,const VkAllocationCallbacks *a,VkFramebuffer *p){auto r=createFramebuffer(d,i,a,p);if(r==VK_SUCCESS)frameViews[*p]={i->pAttachments,i->pAttachments+i->attachmentCount};return r;}
static void boundPipeline(VkCommandBuffer c,VkPipelineBindPoint b,VkPipeline p){assert(inside && layouts.at(activePass)==layouts.at(pipelinePasses.at(p)));++pipelineBinds;bindPipeline(c,b,p);}
static void updated(VkDevice d,uint32_t n,const VkWriteDescriptorSet *w,uint32_t nc,const VkCopyDescriptorSet *cp){for(unsigned x=0;x<n;++x)inputViews[w[x].dstSet].clear();for(unsigned x=0;x<n;++x)if(w[x].descriptorType==VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT){assert(w[x].descriptorCount==1 && w[x].pImageInfo->imageLayout==VK_IMAGE_LAYOUT_GENERAL);inputViews[w[x].dstSet].push_back(w[x].pImageInfo->imageView);}updateSets(d,n,w,nc,cp);}
static void boundSets(VkCommandBuffer c,VkPipelineBindPoint b,VkPipelineLayout l,uint32_t first,uint32_t n,const VkDescriptorSet *s,uint32_t nd,const uint32_t *o){assert(first==0 && n==1);activeSet=*s;++setBinds;bindSets(c,b,l,first,n,s,nd,o);}
static void copied(VkCommandBuffer c,VkBuffer b,VkImage i,VkImageLayout l,uint32_t n,const VkBufferImageCopy *r){assert(!inside);for(unsigned x=0;x<n;++x)assert(imageLayouts.at(i).at(r[x].imageSubresource.mipLevel)==l);++copies;copyImage(c,b,i,l,n,r);}
static void formats(VkPhysicalDevice d,VkFormat f,VkFormatProperties *p){++formatQueries;formatProperties(d,f,p);if(getenv("MERGE_TEST_SHADOW"))p->linearTilingFeatures&=~VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT;}
static VkResult extensions(VkPhysicalDevice d,const char *layer,uint32_t *n,VkExtensionProperties *p){auto r=enumerateExtensions(d,layer,n,p);if(p && getenv("MERGE_TEST_NO_ORDER")){for(unsigned x=0;x<*n;)if(!strcmp(p[x].extensionName,VK_EXT_RASTERIZATION_ORDER_ATTACHMENT_ACCESS_EXTENSION_NAME)){p[x]=p[--*n];}else ++x;}return r;}
static void begin(VkCommandBuffer c,const VkRenderPassBeginInfo *i,VkSubpassContents s){assert(!inside);inside=true;activePass=i->renderPass;activeFrame=i->framebuffer;activeSet={};++passes;auto a=actions.at(i->renderPass);lastLoad=a.first;lastStore=a.second;beginPass(c,i,s);}
static void end(VkCommandBuffer c){assert(inside);inside=false;endPass(c);}
static void barrier(VkCommandBuffer c,VkPipelineStageFlags a,VkPipelineStageFlags b,VkDependencyFlags f,uint32_t nm,const VkMemoryBarrier *m,uint32_t nb,const VkBufferMemoryBarrier *bb,uint32_t ni,const VkImageMemoryBarrier *ib){assert(!inside);++barrierCalls;imageBarrierCalls+=ni!=0;imageBarriers+=ni;for(unsigned x=0;x<ni;++x)for(unsigned l=ib[x].subresourceRange.baseMipLevel;l<ib[x].subresourceRange.baseMipLevel+ib[x].subresourceRange.levelCount;++l)imageLayouts[ib[x].image].at(l)=ib[x].newLayout;pipelineBarrier(c,a,b,f,nm,m,nb,bb,ni,ib);}
static void drawn(VkCommandBuffer c,uint32_t n,uint32_t i,uint32_t f,uint32_t fi){assert(inside);if(activeSet)for(auto view:inputViews[activeSet]){auto &views=frameViews.at(activeFrame);assert(!views.empty() && view==views[0]);}++draws;draw(c,n,i,f,fi);}
static VkResult start(VkCommandBuffer c,const VkCommandBufferBeginInfo *i){inside=false;return beginCommand(c,i);}
static VkResult finish(VkCommandBuffer c){assert(!inside);return endCommand(c);}
static PFN_vkVoidFunction getDevice(VkDevice d,const char *name){auto f=deviceProc(d,name);
#define WRAP(n,var,fn) if(!strcmp(name,"vk" #n)){var=reinterpret_cast<PFN_vk##n>(f);return reinterpret_cast<PFN_vkVoidFunction>(fn);}
 WRAP(CmdSetViewport,setViewport,viewport);WRAP(CmdSetScissor,setScissor,scissor);
 WRAP(QueueSubmit,queueSubmit,submit);WRAP(WaitForFences,waitFences,waited);WRAP(CmdBeginRenderPass,beginPass,begin);WRAP(CmdEndRenderPass,endPass,end);WRAP(CmdPipelineBarrier,pipelineBarrier,barrier);WRAP(CmdDraw,draw,drawn);WRAP(BeginCommandBuffer,beginCommand,start);WRAP(EndCommandBuffer,endCommand,finish);WRAP(CreateRenderPass,createPass,create);
 WRAP(CreateGraphicsPipelines,createPipelines,pipelines);WRAP(CreateFramebuffer,createFramebuffer,framebuffer);WRAP(CmdBindPipeline,bindPipeline,boundPipeline);WRAP(UpdateDescriptorSets,updateSets,updated);WRAP(CmdBindDescriptorSets,bindSets,boundSets);WRAP(CmdCopyBufferToImage,copyImage,copied);
 return f;
}
extern "C" PFN_vkVoidFunction vk_icdGetInstanceProcAddr(VkInstance i,const char *name){
 if(!instanceProc){auto *lib=dlopen(getenv("SOFTWARE_ICD"),RTLD_NOW|RTLD_LOCAL);assert(lib);instanceProc=reinterpret_cast<PFN_vkGetInstanceProcAddr>(dlsym(lib,"vk_icdGetInstanceProcAddr"));assert(instanceProc);}
 auto f=instanceProc(i,name);if(!strcmp(name,"vkGetDeviceProcAddr")){deviceProc=reinterpret_cast<PFN_vkGetDeviceProcAddr>(f);return reinterpret_cast<PFN_vkVoidFunction>(getDevice);}
 if(!strcmp(name,"vkGetPhysicalDeviceFormatProperties")){formatProperties=reinterpret_cast<PFN_vkGetPhysicalDeviceFormatProperties>(f);return reinterpret_cast<PFN_vkVoidFunction>(formats);}
 if(!strcmp(name,"vkEnumerateDeviceExtensionProperties")){enumerateExtensions=reinterpret_cast<PFN_vkEnumerateDeviceExtensionProperties>(f);return reinterpret_cast<PFN_vkVoidFunction>(extensions);}return f;
}
