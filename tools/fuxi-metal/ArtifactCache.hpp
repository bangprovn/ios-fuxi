/* Original bounded translation store. Only the index is resident. Immutable
 * snapshot/spool descriptors pin payloads across replacement and concurrent IO.
 * Never serialize library/AIR inputs. No cache lock spans translator work. */
#pragma once
#include "PipelineCache.hpp"
#include "PrewarmStore.hpp"
#include <algorithm>
#include <memory>
#include <mutex>
namespace fuxi {
class ArtifactCache {
 struct File {int fd;explicit File(int n):fd(n){}~File(){if(fd>=0)::close(fd);}File(const File&)=delete;};
 struct Entry {std::array<uint8_t,32> key;std::shared_ptr<File> file;size_t offset,length;uint32_t crc;bool verified=false;};
 std::vector<Entry> entries;
 std::array<uint8_t,128> header{};
 mutable std::mutex mutex;std::mutex saving;
 int directory=-1;std::string filename;
 std::shared_ptr<File> pending;size_t pendingBytes=0;
 size_t used=0;uint64_t revision=0,savedRevision=0;
 int (*verify)(const uint8_t *,const uint8_t *,size_t)=nullptr;
 uint64_t hits=0,misses=0,rejected=0;
 static inline std::atomic<unsigned> sequence{0};
 static uint32_t word(const uint8_t *p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;}
 static void put(uint8_t *p,uint32_t v){for(unsigned i=0;i<4;++i)p[i]=uint8_t(v>>(8*i));}
 static uint32_t crc(uint32_t c,const uint8_t *p,size_t n){
  // IEEE CRC32, slicing by eight; identical to v1 snapshots. No CPU extension.
  static const auto table=[] {
   std::array<std::array<uint32_t,256>,8> t{};
   for(unsigned i=0;i<256;++i){uint32_t v=i;for(unsigned j=0;j<8;++j)v=(v>>1)^(0xedb88320u&uint32_t(-int32_t(v&1)));t[0][i]=v;}
   for(unsigned j=1;j<8;++j)for(unsigned i=0;i<256;++i){uint32_t v=t[j-1][i];t[j][i]=(v>>8)^t[0][v&255];}return t;
  }();
  while(n>=8){uint32_t a=word(p)^c,b=word(p+4);c=table[7][a&255]^table[6][(a>>8)&255]^table[5][(a>>16)&255]^table[4][a>>24]^table[3][b&255]^table[2][(b>>8)&255]^table[1][(b>>16)&255]^table[0][b>>24];p+=8;n-=8;}
  while(n--)c=table[0][(c^*p++)&255]^(c>>8);return c;
 }
 static bool transfer(int fd,void *p,size_t n,size_t offset,bool writing){
  auto *b=(uint8_t *)p;while(n){ssize_t k=writing ? ::pwrite(fd,b,n,off_t(offset)) : ::pread(fd,b,n,off_t(offset));if(k<0 && errno==EINTR)continue;if(k<=0)return false;b+=k;n-=size_t(k);offset+=size_t(k);}return true;
 }
 std::shared_ptr<File> temporary(const char *suffix,bool anonymous,std::string &name) {
  char tail[80];snprintf(tail,sizeof(tail),".%s.%lu.%u",suffix,(unsigned long)getpid(),sequence++);name=anonymous?filename+tail:filename+".tmp";
  auto file=std::make_shared<File>(-1);
  int fd=openat(directory,name.c_str(),O_RDWR|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);file->fd=fd;if(fd<0)return {};
  if(!privateStorage(fd,false,geteuid())){unlinkat(directory,name.c_str(),0);return {};}
  if(anonymous && unlinkat(directory,name.c_str(),0))return {};
  return file;
 }
 const char *load() {
  auto file=std::make_shared<File>(-1);
  int fd=openat(directory,filename.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);file->fd=fd;
  if(fd<0)return errno==ENOENT?nullptr:"open";
  struct stat s{};if(!privateStorage(fd,false,geteuid()))return "permissions";
  if(fstat(fd,&s) || s.st_size<off_t(header.size()) || s.st_size>off_t(header.size()+maxBytes))return "length";
  auto h=header;if(!transfer(fd,h.data(),h.size(),0,false))return "read";
  if(memcmp(h.data(),header.data(),16) || memcmp(h.data()+28,header.data()+28,100))return "identity";
  size_t n=word(h.data()+16),count=word(h.data()+24);
  if(n>maxBytes || n+header.size()!=size_t(s.st_size) || count>maxEntries)return "length";
  std::vector<Entry> decoded;size_t at=header.size();uint32_t checksum=~0u;std::array<uint8_t,65536> chunk{};
  std::vector<uint8_t> verification;
  for(size_t i=0;i<count;++i){
   if(size_t(s.st_size)-at<36)return "record";
   std::array<uint8_t,36> record{};if(!transfer(fd,record.data(),record.size(),at,false))return "read";
   checksum=crc(checksum,record.data(),record.size());Entry e{};memcpy(e.key.data(),record.data(),32);e.length=word(record.data()+32);at+=36;e.offset=at;e.file=file;
   if(!e.length || e.length>maxEntry || e.length>size_t(s.st_size)-at || std::any_of(decoded.begin(),decoded.end(),[&](const Entry &x){return x.key==e.key;}))return "record";
   if(verify)verification.resize(e.length);size_t copied=0;
   uint32_t check=~0u;
   for(size_t left=e.length;left;){size_t k=std::min(left,chunk.size());if(!transfer(fd,chunk.data(),k,at,false))return "read";checksum=crc(checksum,chunk.data(),k);check=crc(check,chunk.data(),k);if(verify){memcpy(verification.data()+copied,chunk.data(),k);copied+=k;}at+=k;left-=k;}
   e.verified=verify && !isRecipeKey(e.key.data()) && verify(e.key.data(),verification.data(),verification.size())==1;
   e.crc=~check;decoded.push_back(std::move(e));
  }
  if(at!=size_t(s.st_size))return "record";
  struct stat end{};if(fstat(fd,&end) || end.st_size!=s.st_size)return "length";
  if(~checksum!=word(h.data()+20))return "checksum";
  // Nothing becomes visible until the entire index, payload and CRC are valid.
  entries=std::move(decoded);used=n;return nullptr;
 }
 bool save(bool &busy) {
  std::vector<Entry> snapshot;uint64_t generation;
  {std::lock_guard<std::mutex> lock(mutex);if(directory<0 || revision==savedRevision)return true;snapshot=entries;generation=revision;}
  CacheWriteLease lease(directory);if(lease.fd<0){busy=lease.busy;return false;}
  unlinkat(directory,(filename+".tmp").c_str(),0); // Orphan from the v1 writer.
  // Preserve v1 format: old files can be indexed lazily with the larger cap.
  std::string temp;auto file=temporary("tmp",false,temp);if(!file)return false;
  struct Cleanup {int dir;std::string name;~Cleanup(){unlinkat(dir,name.c_str(),0);}} cleanup{directory,temp};
  auto original=snapshot;
  size_t at=header.size();uint32_t checksum=~0u;std::array<uint8_t,65536> chunk{};
  for(auto &e:snapshot){
   std::array<uint8_t,36> record{};memcpy(record.data(),e.key.data(),32);put(record.data()+32,uint32_t(e.length));
   if(!transfer(file->fd,record.data(),record.size(),at,true))return false;checksum=crc(checksum,record.data(),record.size());at+=record.size();
   size_t source=e.offset,dest=at;uint32_t check=~0u;
   for(size_t left=e.length;left;){size_t k=std::min(left,chunk.size());if(!transfer(e.file->fd,chunk.data(),k,source,false) || !transfer(file->fd,chunk.data(),k,at,true))return false;checksum=crc(checksum,chunk.data(),k);check=crc(check,chunk.data(),k);source+=k;at+=k;left-=k;}
   if(~check!=e.crc)return false; // Reject modification/truncation after initial load.
   e.file=file;e.offset=dest;
  }
  auto h=header;put(h.data()+16,uint32_t(at-header.size()));put(h.data()+20,~checksum);put(h.data()+24,uint32_t(snapshot.size()));
  if(!transfer(file->fd,h.data(),h.size(),0,true) || fsync(file->fd) || renameat(directory,temp.c_str(),directory,filename.c_str()) || fsync(directory))return false;
  {std::lock_guard<std::mutex> lock(mutex);
   // Remap saved entries even if another thread appended while saving. Pin
   // the old descriptor for readers; preserve LRU and newer replacements.
   for(auto &e:entries)for(size_t i=0;i<original.size();++i)if(e.key==original[i].key && e.file==original[i].file && e.offset==original[i].offset){e.file=snapshot[i].file;e.offset=snapshot[i].offset;break;}
   if(revision==generation){
    savedRevision=generation;pending.reset();pendingBytes=0;
   }
  }
  return true;
 }
public:
 static constexpr size_t maxBytes=256*1024*1024,maxEntry=8*1024*1024,maxEntries=1024;
 struct Access {ArtifactCache &cache;std::vector<uint8_t> bytes;explicit Access(ArtifactCache &c):cache(c){}};
 void open(const VkPhysicalDeviceProperties &p,const VkPhysicalDeviceDriverProperties &driver,const VkPhysicalDeviceIDProperties &id,const uint8_t *translator,const char *client) noexcept {
  if(!translator || !client || !clientName(client))return;
  try {
   filename=std::string(client)+".translations";
   memcpy(header.data(),"FXMTLA01",8);put(header.data()+8,1);put(header.data()+12,uint32_t(header.size()));put(header.data()+28,uint32_t(driver.driverID));
   put(header.data()+32,p.apiVersion);put(header.data()+36,p.vendorID);put(header.data()+40,p.deviceID);put(header.data()+44,p.driverVersion);
   memcpy(header.data()+48,p.pipelineCacheUUID,16);memcpy(header.data()+64,id.driverUUID,16);memcpy(header.data()+80,id.deviceUUID,16);memcpy(header.data()+96,translator,32);
#ifdef FUXI_MTL_HOST_TEST
   const char *path=getenv("FUXI_MTL_TEST_CACHE_DIR");if(path)directory=::open(path,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
#else
   directory=openClientStorage(client);
#endif
   if(!privateStorage(directory,true,geteuid())){close();fprintf(stderr,"FUXI MTL: translation cache unavailable (storage)\n");return;}
   auto reason=load();
   fprintf(stderr,"FUXI MTL: translation cache loaded %zu bytes / %zu pipelines result=%s%s%s\n",used,entries.size(),reason?"discard(":"ready",reason?reason:"",reason?")":"");
  }catch(...){close();fprintf(stderr,"FUXI MTL: translation cache unavailable (allocation)\n");}
 }
 void setVerifier(int (*fn)(const uint8_t *,const uint8_t *,size_t)){verify=fn;} // Before open; adapter lifetime owns fn.
 static size_t read(void *context,const uint8_t *key,const uint8_t **out) noexcept {return readVerified(context,key,out,nullptr);}
 static size_t readVerified(void *context,const uint8_t *key,const uint8_t **out,uint32_t *verified) noexcept {
  if(verified)*verified=0;
  auto &access=*(Access *)context;auto &c=access.cache;*out=nullptr;
  try {
   Entry entry{};
   {std::lock_guard<std::mutex> lock(c.mutex);
    auto i=std::find_if(c.entries.begin(),c.entries.end(),[&](const Entry &e){return !memcmp(e.key.data(),key,32);});if(i==c.entries.end())return 0;
    std::rotate(i,i+1,c.entries.end());entry=c.entries.back();
   }
   access.bytes.resize(entry.length);
   if(!transfer(entry.file->fd,access.bytes.data(),entry.length,entry.offset,false) || ~crc(~0u,access.bytes.data(),access.bytes.size())!=entry.crc)return 0;
   if(verified)*verified=entry.verified?1:0;*out=access.bytes.data();return access.bytes.size();
  }catch(...){return 0;}
 }
 static void write(void *context,const uint8_t *key,const uint8_t *bytes,size_t n) noexcept {
  auto &c=((Access *)context)->cache;if(!n || n>maxEntry)return;
  try {
   // Bound the anonymous append spool even if maintenance is unavailable.
   bool compact;{std::lock_guard<std::mutex> lock(c.mutex);compact=c.pendingBytes+n>maxBytes;}
   if(compact)c.checkpoint();
   uint32_t checksum=~crc(~0u,bytes,n);bool verified=c.verify && !isRecipeKey(key) && c.verify(key,bytes,n)==1;
   Entry e{};memcpy(e.key.data(),key,32);e.length=n;e.crc=checksum;e.verified=verified;
   {std::lock_guard<std::mutex> lock(c.mutex);if(c.directory<0 || c.pendingBytes+n>maxBytes)return;
    if(!c.pending){std::string ignored;c.pending=c.temporary("pending",true,ignored);if(!c.pending)return;}
    e.file=c.pending;e.offset=c.pendingBytes;c.pendingBytes+=n;
   }
   // Reserve a disjoint append range, then do filesystem IO without blocking
   // readers. A checkpoint can reset pending: this local descriptor pins it.
   if(!transfer(e.file->fd,const_cast<uint8_t *>(bytes),n,e.offset,true))return;
   std::lock_guard<std::mutex> lock(c.mutex);if(c.directory<0)return;
   for(auto i=c.entries.begin();i!=c.entries.end();)if(i->key==e.key){c.used-=36+i->length;i=c.entries.erase(i);}else ++i;
   while(!c.entries.empty() && (c.entries.size()>=maxEntries || c.used+36+n>maxBytes)){c.used-=36+c.entries.front().length;c.entries.erase(c.entries.begin());}
   c.entries.push_back(std::move(e));c.used+=36+n;++c.revision;
  }catch(...){} // Optional cache failure must not fail shader translation.
 }
 static void outcome(void *context,uint32_t value) noexcept {auto &c=((Access *)context)->cache;std::lock_guard<std::mutex> lock(c.mutex);if(value==1)++c.hits;else {++c.misses;if(value==2)++c.rejected;}}
 // Payloads of the prewarm recipes stored beside the translations (keys with the recipe prefix), oldest first.
 std::vector<std::vector<uint8_t>> recipes() noexcept {
  std::vector<std::vector<uint8_t>> out;
  try {
   std::vector<Entry> list;{std::lock_guard<std::mutex> lock(mutex);for(auto &e:entries)if(isRecipeKey(e.key.data()))list.push_back(e);}
   for(auto &e:list){std::vector<uint8_t> bytes(e.length);if(!transfer(e.file->fd,bytes.data(),e.length,e.offset,false) || ~crc(~0u,bytes.data(),bytes.size())!=e.crc)continue;out.push_back(std::move(bytes));}
  }catch(...){}
  return out;
 }
 bool available() const {std::lock_guard<std::mutex> lock(mutex);return directory>=0;}
 void checkpoint() noexcept {
  std::unique_lock<std::mutex> writer(saving,std::try_to_lock);if(!writer.owns_lock())return;
  {std::lock_guard<std::mutex> lock(mutex);if(directory<0 || revision==savedRevision)return;}
  bool ok=false,busy=false;try{ok=save(busy);}catch(...){}
  if(busy)return;
  std::lock_guard<std::mutex> lock(mutex);
  fprintf(stderr,"FUXI MTL: translation cache saved %zu bytes / %zu pipelines result=%s\n",ok?used:0,entries.size(),ok?"saved":"memory-only");
  if(!ok){::close(directory);directory=-1;}
 }
 void report() noexcept {std::lock_guard<std::mutex> lock(mutex);
  fprintf(stderr,"FUXI MTL: cache period translation hits=%llu misses=%llu rejected=%llu\n",(unsigned long long)hits,(unsigned long long)misses,(unsigned long long)rejected);hits=misses=rejected=0;
 }
 void close() noexcept {std::lock_guard<std::mutex> writer(saving);std::lock_guard<std::mutex> lock(mutex);if(directory>=0)::close(directory);directory=-1;entries.clear();pending.reset();used=pendingBytes=0;revision=savedRevision=0;}
 ~ArtifactCache(){close();}
 ArtifactCache()=default;ArtifactCache(const ArtifactCache&)=delete;ArtifactCache &operator=(const ArtifactCache&)=delete;
};
}
