/* FUXI_MTL_DESC_REUSE index and pool-reset rule (DescriptorReuse.hpp), without Vulkan. */
#include "DescriptorReuse.hpp"
#include <cassert>
#include <cstdio>
struct V {uint64_t view,buffer,offset;bool operator==(const V &b) const {return view==b.view && buffer==b.buffer && offset==b.offset;}};
int main(){
 using R=fuxi::DescriptorReuse<V,int,4>;R r;uint64_t probes=0;
 V a[2]={{10,20,0},{11,20,256}},b[2]={{10,20,0},{11,20,512}},c[1]={{12,0,0}};
 auto handles=[](const V *v,unsigned n,uint64_t layout,std::vector<uint64_t> &h){h={layout};for(unsigned i=0;i<n;++i){h.push_back(v[i].view);h.push_back(v[i].buffer);}};
 std::vector<uint64_t> h;
 handles(a,2,7,h);r.insert(7,2,99,a,1,h.data(),h.size());
 handles(b,2,7,h);r.insert(7,2,99,b,2,h.data(),h.size()); // same hash, other values: the full compare tells them apart
 handles(c,1,8,h);r.insert(8,1,5,c,3,h.data(),h.size());
 assert(r.live()==3 && r.pools[0].sets==3);
 assert(r.find(7,2,99,a,probes)->set==1 && r.find(7,2,99,b,probes)->set==2 && r.find(8,1,5,c,probes)->set==3);
 assert(!r.find(8,2,99,a,probes) && !r.find(7,1,99,a,probes) && !r.find(7,2,98,a,probes)); // layout, count and hash must all match
 V a2[2]={a[0],{11,20,257}};assert(!r.find(7,2,99,a2,probes));
 // Destroying buffer 20 drops both sets that name it (it is listed once per set), and nothing else.
 assert(r.forget(20)==2 && r.live()==1 && !r.find(7,2,99,a,probes) && !r.find(7,2,99,b,probes) && r.find(8,1,5,c,probes));
 assert(r.forget(20)==0 && r.forget(10)==0 && r.forget(0)==0); // already gone; zero handles are never indexed
 assert(r.forget(8)==1 && r.live()==0); // the set layout handle counts as one of the names
 // A new object with an old handle value finds nothing until a set is made for it.
 handles(a,2,7,h);r.insert(7,2,99,a,4,h.data(),h.size());assert(r.find(7,2,99,a,probes)->set==4);
 // Pool reset: only when the last bind is older than every pending command buffer.
 R::Pool p;p.lastUse=5;assert(!R::idle(p,5) && !R::idle(p,3) && R::idle(p,6));p.lastUse=0;assert(R::idle(p,1));
 r.clear();assert(!r.live() && r.entries.empty() && r.byHandle.empty());
 puts("PASS: descriptor reuse index: full-value match, destroy-time forget by any named handle, idle rule");
}
