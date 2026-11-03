/* FUXI_MTL_BIND_SKIP rules (BindTracker.hpp), without Vulkan. */
#include "BindTracker.hpp"
#include <cassert>
#include <cstdio>
int main(){
 fuxi::BindTracker t;uint8_t a[16]={1,2,3,4},b[16]={1,2,3,5},c[8]={9},big[132]={};
 // A repeat with the same layout, range and bytes is left out; other bytes, ranges or stages are recorded.
 assert(!t.push(7,16,0,16,a) && t.push(7,16,0,16,a) && !t.push(7,16,0,16,b) && !t.push(7,16,0,16,a) && t.push(7,16,0,16,a));
 assert(!t.push(7,1,16,8,c) && t.push(7,1,16,8,c) && t.push(7,16,0,16,a)); // a disjoint range keeps the other
 assert(!t.push(7,1,0,16,a) && t.push(7,1,0,16,a) && !t.push(7,16,0,16,a)); // same bytes, other stages: recorded, and it replaced the range
 // A push that overlaps a tracked range drops it even when it does not cover it.
 assert(t.push(7,16,0,16,a) && !t.push(7,16,8,4,c) && !t.push(7,16,0,16,a));
 // Another pipeline layout forgets every push; so does reset().
 assert(t.push(7,16,0,16,a) && !t.push(8,16,0,16,a) && t.push(8,16,0,16,a) && !t.push(7,16,0,16,a));
 t.reset();assert(!t.push(7,16,0,16,a) && t.push(7,16,0,16,a));
 // A push over 128 bytes is never tracked, but still clears what it overlaps.
 assert(!t.push(7,16,0,132,big) && !t.push(7,16,0,132,big) && !t.push(7,16,0,16,a));
 // Four ranges are tracked; a fifth is recorded every time.
 t.reset();for(uint32_t i=0;i<4;++i)assert(!t.push(7,16,i*16,16,a));for(uint32_t i=0;i<4;++i)assert(t.push(7,16,i*16,16,a));
 assert(!t.push(7,16,64,16,a) && !t.push(7,16,64,16,a));
 // Vertex buffers per binding number: buffer and offset must both match.
 assert(!t.vertexBuffer(0,100,0) && t.vertexBuffer(0,100,0) && !t.vertexBuffer(0,100,48) && !t.vertexBuffer(0,100,0) && !t.vertexBuffer(1,100,0) && t.vertexBuffer(0,100,0) && t.vertexBuffer(1,100,0) && !t.vertexBuffer(0,101,0));
 for(uint32_t i=2;i<8;++i)assert(!t.vertexBuffer(i,5,0));assert(!t.vertexBuffer(8,5,0) && !t.vertexBuffer(8,5,0)); // a ninth binding is not tracked
 // Index buffer: buffer, offset and type.
 assert(!t.indexBufferBind(200,0,0) && t.indexBufferBind(200,0,0) && !t.indexBufferBind(200,16,0) && !t.indexBufferBind(200,16,1) && !t.indexBufferBind(201,16,1) && t.indexBufferBind(201,16,1));
 t.reset();assert(!t.vertexBuffer(0,101,0) && !t.indexBufferBind(201,16,1));
 puts("PASS: bind skip tracker: exact repeats only, overlap and layout invalidation, untracked overflow, reset per command buffer");
}
