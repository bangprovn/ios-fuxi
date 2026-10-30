/* Real Vulkan deferred retirement, independent slots, pins and fault quarantine. */
#include "Backend.hpp"
#include <cassert>
#include <future>
#include <atomic>
int main(int argc,char **argv){
 using namespace fuxi;
 if(argc>1 && !strcmp(argv[1],"host")){
 Backend b;auto *a=b.allocate(4096),*c=b.allocate(4096);std::function<void()> x,y;
 b.submit({{nullptr,a,0,0,4096,63}},{},&x);std::promise<void> started;
 auto next=std::async(std::launch::async,[&]{started.set_value();b.submit({{a,c,0,0,4096,0}},{},&y);});
 started.get_future().wait();assert(next.wait_for(std::chrono::milliseconds(30))==std::future_status::timeout);x();next.get();y();
 assert(b.flushCalls>=3 && b.invalidateCalls>=3);for(unsigned i=0;i<4096;++i)assert(((uint8_t *)c->contents)[i]==63);
 b.destroy(a);b.destroy(c);puts("PASS: shared noncoherent allocation drains before flush/record/invalidate");return 0;}
 if(argc>1){Backend lost;auto *a=lost.allocate(4096),*b=lost.allocate(4096);std::function<void()> x,y;unsigned fired=0;auto refused=Backend::earlyRefused.load();
 lost.submit({{nullptr,a,0,0,4096,1}},{},&x,{},nullptr,[&]{++fired;});lost.submit({{nullptr,b,0,0,4096,2}},{},&y,{},nullptr,[&]{++fired;});auto held=liveMemoryObjects.load();
 for(auto *ticket:{&x,&y}){bool failed=false;try{(*ticket)();}catch(const std::runtime_error&){failed=true;}assert(failed && lost.poisoned);}
 assert(!fired && Backend::earlyRefused==refused+2); // a lost/timed-out or poisoned fence never releases leases early
 lost.destroy(a);lost.destroy(b);lost.close();assert(liveMemoryObjects==held);puts("PASS: injected in-flight fence loss/timeout, both tickets fail, no early release, DMA quarantine");return 0;}
 Backend b;auto baseline=liveMemoryObjects.load();
 auto *first=b.allocate(4096),*second=b.allocate(4096);
 std::function<void()> retireFirst,retireSecond;unsigned scheduled=0;
 FrameSample a,c;
 {FrameTraceScope scope(&a);b.submit({{nullptr,first,0,0,4096,31}},[&]{++scheduled;},&retireFirst);}
 {FrameTraceScope scope(&c);b.submit({{first,second,0,0,4096,0}},[&]{++scheduled;},&retireSecond);}
 assert(scheduled==2 && retireFirst && retireSecond && !a.gpuWait && !c.gpuWait);
 assert(b.reuseCounts.commandPools==2 && b.reuseCounts.fences==2);
 // Third submission cannot reset either live slot. This is a synchronization
 // assertion, not a GPU speed threshold (neither ticket has been retired).
 std::promise<void> started;auto third=std::async(std::launch::async,[&]{started.set_value();b.submit({{nullptr,first,0,0,4,7}});});
 started.get_future().wait();assert(third.wait_for(std::chrono::milliseconds(30))==std::future_status::timeout);
 {FrameTraceScope scope(&a);retireFirst();}third.get();
 {FrameTraceScope scope(&c);retireSecond();}
 assert(a.gpuWait && c.gpuWait && b.reuseCounts.commandPools==2);
 for(unsigned i=0;i<4096;++i)assert(((uint8_t *)second->contents)[i]==31);
 // External destruction cannot reclaim a buffer while a ticket owns it.
 auto *pinned=b.allocate(4096);std::function<void()> retire;
 b.submit({{nullptr,pinned,0,0,4096,42}},{},&retire);auto held=liveMemoryObjects.load();b.destroy(pinned);assert(liveMemoryObjects==held);retire();assert(liveMemoryObjects==held-1);
 // FUXI_MTL_EARLY_RELEASE hook: runs after the fence while another thread (the next encode) holds the mutex; cleanup waits for it.
 {auto early=Backend::earlyReleases.load(),locks=Backend::retireLockCount.load();std::atomic<bool> fired{false},done{false};std::function<void()> ticket;
  b.submit({{nullptr,first,0,0,4096,77}},{},&ticket,{},nullptr,[&]{assert(!done);fired=true;});
  std::unique_lock<std::mutex> encode(b.mutex);auto retiring=std::async(std::launch::async,[&]{ticket();done=true;});
  for(unsigned i=0;i<2000 && !fired;++i)std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(fired && retiring.wait_for(std::chrono::milliseconds(30))==std::future_status::timeout && !done);
  encode.unlock();retiring.get();assert(done && Backend::earlyReleases==early+1 && Backend::retireLockCount==locks+1);
  for(unsigned i=0;i<4096;++i)assert(((uint8_t *)first->contents)[i]==77);
  // Without the hook nothing is released early; the wait is still counted.
  b.submit({{nullptr,first,0,0,4096,78}},{},&ticket);ticket();assert(Backend::earlyReleases==early+1 && Backend::retireLockCount==locks+2);
  for(unsigned i=0;i<4096;++i)assert(((uint8_t *)first->contents)[i]==78);}
 b.destroy(first);b.destroy(second);b.close();assert(liveMemoryObjects==baseline);
 // A poison arriving after two successful QueueSubmits must fail both tickets,
 // free admission slots, and leave DMA allocations quarantined through close.
 Backend lost;first=lost.allocate(4096);second=lost.allocate(4096);
 lost.submit({{nullptr,first,0,0,4096,1}},{},&retireFirst);lost.submit({{nullptr,second,0,0,4096,2}},{},&retireSecond);
 held=liveMemoryObjects.load();try{lost.loseWhileInFlightForTest();assert(false);}catch(const std::runtime_error&){}assert(lost.poisoned);
 for(auto *ticket:{&retireFirst,&retireSecond}){bool failed=false;try{(*ticket)();}catch(const std::runtime_error&){failed=true;}assert(failed);}
 lost.destroy(first);lost.destroy(second);lost.close();assert(liveMemoryObjects==held);
 puts("PASS: deferred wait, two independent lanes, bounded third submit, GPU ordering/pixels, resource pins, early release before the mutex, poison-in-flight quarantine");
}
