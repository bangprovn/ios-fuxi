/* FUXI_MTL_BIND_SKIP: push constant, vertex buffer and index buffer calls that would set what one command buffer already holds. */
#pragma once
#include <array>
#include <cstdint>
#include <cstring>
namespace fuxi {
// Push constants and vertex/index buffer bindings are command buffer state: Vulkan keeps them across render passes and pipeline binds
// until the command buffer is reset (Turnip keeps cmd->push_constants, state.vb and the index state and re-emits them as draw state for
// each pass and pipeline). A call is left out only when it would set exactly what was set last in this command buffer:
//  - push constants: the same pipeline layout handle as every tracked push (a push with another layout clears them all), the same stages,
//    offset and size, the same bytes; a push that is recorded first drops every tracked range its bytes overlap, whatever the stages;
//  - vertex buffer: the same binding number, buffer handle and offset;
//  - index buffer: the same buffer handle, offset and index type.
// Values that do not fit (a fifth push range, a ninth binding, a push over 128 bytes) are not tracked, so they are always recorded.
// reset() whenever recording starts in a command buffer (each split-submit chunk too).
class BindTracker {
 struct Push {uint32_t stages,offset,size;uint8_t bytes[128];};
 struct Vertex {uint32_t binding;uint64_t buffer,offset;};
 uint64_t layout=0;std::array<Push,4> pushes{};unsigned pushCount=0;
 std::array<Vertex,8> vertex{};unsigned vertexCount=0;
 uint64_t indexBuffer=0,indexOffset=0;uint32_t indexType=0;bool indexBound=false;
public:
 void reset(){layout=0;pushCount=0;vertexCount=0;indexBound=false;}
 // true: leave the push out. false: record it; the tracker now holds it.
 bool push(uint64_t pipelineLayout,uint32_t stages,uint32_t offset,uint32_t size,const uint8_t *bytes) {
  if(pipelineLayout!=layout){layout=pipelineLayout;pushCount=0;}
  for(unsigned i=0;i<pushCount;++i){auto &p=pushes[i];if(p.stages==stages && p.offset==offset && p.size==size && !memcmp(p.bytes,bytes,size))return true;}
  unsigned kept=0;for(unsigned i=0;i<pushCount;++i){auto &p=pushes[i];if(p.offset>=uint64_t(offset)+size || offset>=uint64_t(p.offset)+p.size)pushes[kept++]=p;}pushCount=kept;
  if(size<=sizeof(Push::bytes) && pushCount<pushes.size()){auto &p=pushes[pushCount++];p.stages=stages;p.offset=offset;p.size=size;memcpy(p.bytes,bytes,size);}
  return false;
 }
 bool vertexBuffer(uint32_t binding,uint64_t buffer,uint64_t offset) {
  for(unsigned i=0;i<vertexCount;++i)if(vertex[i].binding==binding){if(vertex[i].buffer==buffer && vertex[i].offset==offset)return true;vertex[i].buffer=buffer;vertex[i].offset=offset;return false;}
  if(vertexCount<vertex.size())vertex[vertexCount++]={binding,buffer,offset};
  return false;
 }
 bool indexBufferBind(uint64_t buffer,uint64_t offset,uint32_t type) {
  if(indexBound && indexBuffer==buffer && indexOffset==offset && indexType==type)return true;
  indexBuffer=buffer;indexOffset=offset;indexType=type;indexBound=true;return false;
 }
};
}
