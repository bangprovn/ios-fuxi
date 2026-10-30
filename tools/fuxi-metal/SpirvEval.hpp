/* Concrete CPU evaluator for the vertex stage of a SPIR-V module: runs the entry point for chosen vertex indices on real inputs (vertex attributes, uniform/storage bytes, push constants,
   specialization data) and returns gl_Position. Used only to PROVE screen coverage for occlusion culling; any opcode, type or access it does not model fails the proof (never guesses). */
#pragma once
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <initializer_list>
#include <iterator>
#include <map>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>
namespace fuxi {
struct VsInputs {
 struct Region {const uint8_t *data=nullptr;size_t size=0;};
 std::map<std::pair<uint32_t,uint32_t>,Region> buffers; // (set,binding) -> bytes
 uint8_t push[128]{};uint32_t pushSize=0;
 std::map<uint32_t,uint32_t> spec; // SpecId -> 32-bit value
 std::function<bool(uint32_t location,uint32_t vertex,uint32_t out[4])> attribute; // raw 32-bit lanes of the attribute as the shader sees them (float bits or integers)
 uint32_t instance=0,baseVertex=0;
};
struct VsResult {bool ok=false;std::string why;std::vector<std::array<float,4>> position;unsigned steps=0;};
// Value words with 16 inline slots: most SPIR-V values are scalars, vectors or 4x4 matrices, and a heap vector per value dominated the evaluation time.
class Words {
 uint32_t n_=0;uint32_t inl_[16];std::vector<uint32_t> big_;
public:
 Words(){}
 Words(size_t n,uint32_t v=0){resize(n,v);}
 Words(std::initializer_list<uint32_t> l){assign(l.begin(),l.end());}
 template<class It> Words(It a,It b){assign(a,b);}
 size_t size() const {return n_;}
 bool empty() const {return !n_;}
 uint32_t *begin(){return n_>16?big_.data():inl_;}
 uint32_t *end(){return begin()+n_;}
 const uint32_t *begin() const {return n_>16?big_.data():inl_;}
 const uint32_t *end() const {return begin()+n_;}
 uint32_t &operator[](size_t i){return begin()[i];}
 uint32_t operator[](size_t i) const {return begin()[i];}
 void resize(size_t n,uint32_t v=0){
  if(n>16){if(n_<=16)big_.assign(inl_,inl_+n_);big_.resize(n,v);}
  else {if(n_>16){std::copy(big_.begin(),big_.begin()+std::min<size_t>(n,n_),inl_);big_.clear();}for(size_t i=n_;i<n;++i)inl_[i]=v;}
  n_=uint32_t(n);}
 template<class It> void assign(It a,It b){size_t n=size_t(std::distance(a,b));if(n>16){big_.assign(a,b);}else {big_.clear();std::copy(a,b,inl_);}n_=uint32_t(n);}
 void push_back(uint32_t x){resize(n_+1,x);}
 template<class It> void insert(const uint32_t *at,It a,It b){if(at!=end())throw std::logic_error("Words::insert only appends");size_t old=n_,k=size_t(std::distance(a,b));resize(old+k);std::copy(a,b,begin()+old);}
};
class SpirvEval {
 struct Fail {std::string why;};
 [[noreturn]] static void fail(const std::string &s){throw Fail{s};}
 enum {tVoid=19,tBool=20,tInt=21,tFloat=22,tVec=23,tMat=24,tArr=28,tStruct=30,tPtr=32,tFunc=33};
 struct Type {uint32_t kind=0,width=32,sign=0,count=0,elem=0,storage=0;bool runtime=false;Words members;Type()=default;explicit Type(uint32_t k,uint32_t wd=32,uint32_t sg=0):kind(k),width(wd),sign(sg){}};
 struct Ptr {int kind=0;uint32_t var=0,type=0,mstride=0;size_t off=0;VsInputs::Region mem{};}; // kind 1 flat local words, 2 memory bytes
 struct Val {uint32_t type=0;Words w;Ptr p;};
 struct Dec {uint32_t location=UINT32_MAX,builtin=UINT32_MAX,set=0,binding=0,specId=UINT32_MAX,arrayStride=0;};
 struct MemberDec {uint32_t offset=0,matrixStride=0,builtin=UINT32_MAX;bool rowMajor=false;};
 const uint32_t *w_;size_t n_;uint32_t bound_;
 std::vector<size_t> at_;std::unordered_map<uint32_t,Type> types_;std::unordered_map<uint32_t,Dec> dec_;std::map<std::pair<uint32_t,uint32_t>,MemberDec> mdec_;
 bool relaxed_=false;std::vector<Val> vals_;std::vector<uint32_t> have_;uint32_t gen_=0;std::unordered_map<uint32_t,Words> store_,priv_; // Private/Output/Input/Function variable words
 std::unordered_map<uint32_t,uint32_t> varStorage_,varType_;std::unordered_map<uint32_t,size_t> funcAt_;std::unordered_map<uint32_t,size_t> labelAt_;
 const VsInputs *in_=nullptr;uint32_t vertex_=0;unsigned steps_=0;std::vector<uint32_t> iface_;bool prepared_=false;std::string preparedEntry_;uint32_t entryId_=0;
 const Type &T(uint32_t id) const {auto it=types_.find(id);if(it==types_.end())fail("type "+std::to_string(id));return it->second;}
 size_t words(uint32_t id) const {const Type &t=T(id);switch(t.kind){case tBool:case tInt:case tFloat:if(t.width!=32)fail("non-32-bit scalar");return 1;case tVec:return size_t(t.count)*words(t.elem);case tMat:return size_t(t.count)*words(t.elem);case tArr:if(t.runtime)fail("runtime array value");return size_t(t.count)*words(t.elem);
  case tStruct:{size_t s=0;for(auto m:t.members)s+=words(m);return s;}default:fail("unsized type");}}
 static float f(uint32_t x){float v;memcpy(&v,&x,4);return v;}
 static uint32_t u(float x){uint32_t v;memcpy(&v,&x,4);return v;}
 static int32_t s(uint32_t x){return int32_t(x);}
 const Val &V(uint32_t id) const {if(id>=bound_ || have_[id]!=gen_)fail("undefined value "+std::to_string(id));return vals_[id];}
 void set(uint32_t id,uint32_t type,Words w){vals_[id].type=type;vals_[id].w=std::move(w);vals_[id].p=Ptr{};have_[id]=gen_;}
 // Load a typed value from memory bytes (std140/std430 layout from decorations).
 void loadMem(uint32_t type,const VsInputs::Region &r,size_t off,uint32_t mstride,Words &out) const {
  const Type &t=T(type);
  auto word=[&](size_t o){if(!r.data || o+4>r.size)fail("buffer read out of range");uint32_t x;memcpy(&x,r.data+o,4);out.push_back(x);};
  switch(t.kind){
   case tBool:fail("bool in memory");
   case tInt:case tFloat:if(t.width!=32)fail("non-32-bit memory");word(off);break;
   case tVec:for(uint32_t i=0;i<t.count;++i)loadMem(t.elem,r,off+4*i,0,out);break;
   case tMat:if(!mstride)fail("matrix without stride");for(uint32_t c=0;c<t.count;++c)loadMem(t.elem,r,off+size_t(c)*mstride,0,out);break;
   case tArr:{auto d=dec_.find(type);uint32_t st=d==dec_.end()?0:d->second.arrayStride;if(!st)fail("array without stride");for(uint32_t i=0;i<t.count;++i)loadMem(t.elem,r,off+size_t(i)*st,mstride,out);break;}
   case tStruct:for(size_t m=0;m<t.members.size();++m){auto d=mdec_.find({type,uint32_t(m)});MemberDec md=d==mdec_.end()?MemberDec{}:d->second;if(md.rowMajor)fail("row-major matrix");loadMem(t.members[m],r,off+md.offset,md.matrixStride,out);}break;
   default:fail("memory type");}
 }
 Val walk(const Val &base,uint32_t rt,const uint32_t *idx,uint32_t count){
  Val r=base;r.type=rt;uint32_t cur=base.p.type;
  for(uint32_t i=0;i<count;++i){const Val &iv=V(idx[i]);if(iv.w.size()!=1)fail("access index");uint32_t k=iv.w[0];const Type &t=T(cur);
   if(t.kind==tStruct){if(k>=t.members.size())fail("member index");
    if(r.p.kind==1){size_t o=0;for(uint32_t m=0;m<k;++m)o+=words(t.members[m]);r.p.off+=o;}
    else {auto d=mdec_.find({cur,k});MemberDec md=d==mdec_.end()?MemberDec{}:d->second;if(md.rowMajor)fail("row-major matrix");r.p.off+=md.offset;r.p.mstride=md.matrixStride;}
    cur=t.members[k];}
   else if(t.kind==tArr){if(k>=t.count)fail("array index");
    if(r.p.kind==1)r.p.off+=size_t(k)*words(t.elem);else{auto d=dec_.find(cur);uint32_t st=d==dec_.end()?0:d->second.arrayStride;if(!st)fail("array without stride");r.p.off+=size_t(k)*st;}cur=t.elem;}
   else if(t.kind==tVec){if(k>=t.count)fail("vector index");r.p.off+=r.p.kind==1?k:4*size_t(k);cur=t.elem;}
   else if(t.kind==tMat){if(k>=t.count)fail("matrix index");if(r.p.kind==1)r.p.off+=size_t(k)*words(t.elem);else r.p.off+=size_t(k)*r.p.mstride;cur=t.elem;}
   else fail("access chain into scalar");}
  r.p.type=cur;return r;
 }
 Words loadPtr(const Ptr &p){
  Words out;
  if(p.kind==1){auto &st=store_[p.var];size_t n=words(p.type);if(p.off+n>st.size())fail("local read out of range");out.assign(st.begin()+p.off,st.begin()+p.off+n);}
  else if(p.kind==2)loadMem(p.type,p.mem,p.off,p.mstride,out);
  else fail("load through null pointer");
  return out;
 }
 // Component-wise operations. Returns false when the opcode is not an arithmetic one.
 bool arith(uint32_t op,uint32_t rt,uint32_t id,const uint32_t *a,uint32_t na){
  auto un=[&](auto fn){const Val &x=V(a[0]);Words o(x.w.size());for(size_t i=0;i<o.size();++i)o[i]=fn(x.w[i]);set(id,rt,std::move(o));return true;};
  auto bin=[&](auto fn){const Val &x=V(a[0]),&y=V(a[1]);if(x.w.size()!=y.w.size())fail("operand width");Words o(x.w.size());for(size_t i=0;i<o.size();++i)o[i]=fn(x.w[i],y.w[i]);set(id,rt,std::move(o));return true;};
  switch(op){
   case 126:return un([](uint32_t x){return uint32_t(-s(x));});
   case 127:return un([](uint32_t x){return u(-f(x));});
   case 128:return bin([](uint32_t x,uint32_t y){return x+y;});
   case 129:return bin([](uint32_t x,uint32_t y){return u(f(x)+f(y));});
   case 130:return bin([](uint32_t x,uint32_t y){return x-y;});
   case 131:return bin([](uint32_t x,uint32_t y){return u(f(x)-f(y));});
   case 132:return bin([](uint32_t x,uint32_t y){return x*y;});
   case 133:return bin([](uint32_t x,uint32_t y){return u(f(x)*f(y));});
   case 134:return bin([](uint32_t x,uint32_t y){return y?x/y:0u;});
   case 135:return bin([](uint32_t x,uint32_t y){return y&&!(s(x)==INT32_MIN&&s(y)==-1)?uint32_t(s(x)/s(y)):0u;});
   case 136:return bin([](uint32_t x,uint32_t y){return u(f(x)/f(y));});
   case 137:return bin([](uint32_t x,uint32_t y){return y?x%y:0u;});
   case 138:return bin([](uint32_t x,uint32_t y){return y&&!(s(x)==INT32_MIN&&s(y)==-1)?uint32_t(s(x)%s(y)):0u;});
   case 139:return bin([](uint32_t x,uint32_t y){if(!y||(s(x)==INT32_MIN&&s(y)==-1))return 0u;int32_t m=s(x)%s(y);if(m&&((m<0)!=(s(y)<0)))m+=s(y);return uint32_t(m);});
   case 140:return bin([](uint32_t x,uint32_t y){return u(std::fmod(f(x),f(y)));});
   case 141:return bin([](uint32_t x,uint32_t y){float a_=f(x),b_=f(y);return u(a_-b_*std::floor(a_/b_));});
   case 142:{const Val &v=V(a[0]),&sc=V(a[1]);if(sc.w.size()!=1)fail("scalar operand");Words o(v.w.size());for(size_t i=0;i<o.size();++i)o[i]=u(f(v.w[i])*f(sc.w[0]));set(id,rt,std::move(o));return true;}
   case 143:{const Val &m=V(a[0]),&sc=V(a[1]);Words o(m.w.size());for(size_t i=0;i<o.size();++i)o[i]=u(f(m.w[i])*f(sc.w[0]));set(id,rt,std::move(o));return true;}
   case 144:{const Val &v=V(a[0]),&m=V(a[1]);const Type &mt=T(m.type);uint32_t cols=mt.count,rows=T(mt.elem).count;if(v.w.size()!=rows)fail("vector*matrix");Words o(cols);for(uint32_t c=0;c<cols;++c){float acc=0;for(uint32_t r=0;r<rows;++r)acc+=f(v.w[r])*f(m.w[c*rows+r]);o[c]=u(acc);}set(id,rt,std::move(o));return true;}
   case 145:{const Val &m=V(a[0]),&v=V(a[1]);const Type &mt=T(m.type);uint32_t cols=mt.count,rows=T(mt.elem).count;if(v.w.size()!=cols)fail("matrix*vector");Words o(rows);for(uint32_t r=0;r<rows;++r){float acc=0;for(uint32_t c=0;c<cols;++c)acc+=f(m.w[c*rows+r])*f(v.w[c]);o[r]=u(acc);}set(id,rt,std::move(o));return true;}
   case 146:{const Val &l=V(a[0]),&r=V(a[1]);const Type &lt=T(l.type),&rtp=T(r.type);uint32_t rows=T(lt.elem).count,k=lt.count,cols=rtp.count;if(T(rtp.elem).count!=k)fail("matrix*matrix");Words o(size_t(cols)*rows);for(uint32_t c=0;c<cols;++c)for(uint32_t ro=0;ro<rows;++ro){float acc=0;for(uint32_t i=0;i<k;++i)acc+=f(l.w[i*rows+ro])*f(r.w[c*k+i]);o[c*rows+ro]=u(acc);}set(id,rt,std::move(o));return true;}
   case 148:{const Val &x=V(a[0]),&y=V(a[1]);float acc=0;for(size_t i=0;i<x.w.size();++i)acc+=f(x.w[i])*f(y.w[i]);set(id,rt,{u(acc)});return true;}
   case 109:return un([](uint32_t x){if(!(f(x)>-1.0f && f(x)<4294967040.0f))fail("float to uint range");return uint32_t(std::max(0.0f,f(x)));});
   case 110:return un([](uint32_t x){if(!(f(x)>=-2147483648.0f && f(x)<2147483520.0f))fail("float to int range");return uint32_t(int32_t(f(x)));});
   case 111:return un([](uint32_t x){return u(float(s(x)));});
   case 112:return un([](uint32_t x){return u(float(x));});
   case 124:return un([](uint32_t x){return x;});
   case 194:return bin([](uint32_t x,uint32_t y){return y>=32?0u:x>>y;});
   case 195:return bin([](uint32_t x,uint32_t y){return uint32_t(s(x)>>(y>=32?31:y));});
   case 196:return bin([](uint32_t x,uint32_t y){return y>=32?0u:x<<y;});
   case 197:return bin([](uint32_t x,uint32_t y){return x|y;});
   case 198:return bin([](uint32_t x,uint32_t y){return x^y;});
   case 199:return bin([](uint32_t x,uint32_t y){return x&y;});
   case 200:return un([](uint32_t x){return ~x;});
   case 164:return bin([](uint32_t x,uint32_t y){return uint32_t(x==y);});
   case 165:return bin([](uint32_t x,uint32_t y){return uint32_t(x!=y);});
   case 166:return bin([](uint32_t x,uint32_t y){return x|y;});
   case 167:return bin([](uint32_t x,uint32_t y){return x&y;});
   case 168:return un([](uint32_t x){return uint32_t(!x);});
   case 170:return bin([](uint32_t x,uint32_t y){return uint32_t(x==y);});
   case 171:return bin([](uint32_t x,uint32_t y){return uint32_t(x!=y);});
   case 172:return bin([](uint32_t x,uint32_t y){return uint32_t(x>y);});
   case 173:return bin([](uint32_t x,uint32_t y){return uint32_t(s(x)>s(y));});
   case 174:return bin([](uint32_t x,uint32_t y){return uint32_t(x>=y);});
   case 175:return bin([](uint32_t x,uint32_t y){return uint32_t(s(x)>=s(y));});
   case 176:return bin([](uint32_t x,uint32_t y){return uint32_t(x<y);});
   case 177:return bin([](uint32_t x,uint32_t y){return uint32_t(s(x)<s(y));});
   case 178:return bin([](uint32_t x,uint32_t y){return uint32_t(x<=y);});
   case 179:return bin([](uint32_t x,uint32_t y){return uint32_t(s(x)<=s(y));});
   case 180:case 181:return bin([](uint32_t x,uint32_t y){return uint32_t(f(x)==f(y));});
   case 182:case 183:return bin([](uint32_t x,uint32_t y){return uint32_t(f(x)!=f(y));});
   case 184:case 185:return bin([](uint32_t x,uint32_t y){return uint32_t(f(x)<f(y));});
   case 186:case 187:return bin([](uint32_t x,uint32_t y){return uint32_t(f(x)>f(y));});
   case 188:case 189:return bin([](uint32_t x,uint32_t y){return uint32_t(f(x)<=f(y));});
   case 190:case 191:return bin([](uint32_t x,uint32_t y){return uint32_t(f(x)>=f(y));});
   case 156:return un([](uint32_t x){return uint32_t(f(x)!=f(x));});
   case 157:return un([](uint32_t x){return uint32_t(std::isinf(f(x)));});
   case 169:{if(na<3)fail("select");const Val &c=V(a[0]),&x=V(a[1]),&y=V(a[2]);Words o(x.w.size());for(size_t i=0;i<o.size();++i)o[i]=(c.w.size()==1?c.w[0]:c.w[i])?x.w[i]:y.w[i];set(id,rt,std::move(o));return true;}
   case 154:{const Val &x=V(a[0]);bool r=false;for(auto q:x.w)r|=q!=0;set(id,rt,{uint32_t(r)});return true;}
   case 155:{const Val &x=V(a[0]);bool r=true;for(auto q:x.w)r&=q!=0;set(id,rt,{uint32_t(r)});return true;}
   case 113:case 114:case 115:case 116:case 117:fail("conversion op "+std::to_string(op));
   default:return false;
  }
 }
 void ext(uint32_t rt,uint32_t id,uint32_t op,const uint32_t *a,uint32_t na){
  auto un=[&](auto fn){const Val &x=V(a[0]);Words o(x.w.size());for(size_t i=0;i<o.size();++i)o[i]=fn(x.w[i]);set(id,rt,std::move(o));};
  auto bin=[&](auto fn){const Val &x=V(a[0]),&y=V(a[1]);Words o(x.w.size());for(size_t i=0;i<o.size();++i)o[i]=fn(x.w[i],y.w[y.w.size()==1?0:i]);set(id,rt,std::move(o));};
  auto ter=[&](auto fn){const Val &x=V(a[0]),&y=V(a[1]),&z=V(a[2]);Words o(x.w.size());for(size_t i=0;i<o.size();++i)o[i]=fn(x.w[i],y.w[y.w.size()==1?0:i],z.w[z.w.size()==1?0:i]);set(id,rt,std::move(o));};
  (void)na;
  switch(op){
   case 1:un([](uint32_t x){return u(std::round(f(x)));});break;
   case 2:un([](uint32_t x){return u(std::nearbyint(f(x)));});break;
   case 3:un([](uint32_t x){return u(std::trunc(f(x)));});break;
   case 4:un([](uint32_t x){return u(std::fabs(f(x)));});break;
   case 5:un([](uint32_t x){return uint32_t(std::abs(s(x)));});break;
   case 6:un([](uint32_t x){float v=f(x);return u(v>0?1.f:v<0?-1.f:0.f);});break;
   case 8:un([](uint32_t x){return u(std::floor(f(x)));});break;
   case 9:un([](uint32_t x){return u(std::ceil(f(x)));});break;
   case 10:un([](uint32_t x){return u(f(x)-std::floor(f(x)));});break;
   case 13:un([](uint32_t x){return u(std::sin(f(x)));});break;
   case 14:un([](uint32_t x){return u(std::cos(f(x)));});break;
   case 15:un([](uint32_t x){return u(std::tan(f(x)));});break;
   case 26:bin([](uint32_t x,uint32_t y){return u(std::pow(f(x),f(y)));});break;
   case 27:un([](uint32_t x){return u(std::exp(f(x)));});break;
   case 28:un([](uint32_t x){return u(std::log(f(x)));});break;
   case 29:un([](uint32_t x){return u(std::exp2(f(x)));});break;
   case 30:un([](uint32_t x){return u(std::log2(f(x)));});break;
   case 31:un([](uint32_t x){return u(std::sqrt(f(x)));});break;
   case 32:un([](uint32_t x){return u(1.0f/std::sqrt(f(x)));});break;
   case 37:bin([](uint32_t x,uint32_t y){return u(f(y)<f(x)?f(y):f(x));});break;
   case 38:bin([](uint32_t x,uint32_t y){return std::min(x,y);});break;
   case 39:bin([](uint32_t x,uint32_t y){return uint32_t(std::min(s(x),s(y)));});break;
   case 40:bin([](uint32_t x,uint32_t y){return u(f(x)<f(y)?f(y):f(x));});break;
   case 41:bin([](uint32_t x,uint32_t y){return std::max(x,y);});break;
   case 42:bin([](uint32_t x,uint32_t y){return uint32_t(std::max(s(x),s(y)));});break;
   case 43:ter([](uint32_t x,uint32_t lo,uint32_t hi){float v=f(x);v=v<f(lo)?f(lo):v;v=f(hi)<v?f(hi):v;return u(v);});break;
   case 44:ter([](uint32_t x,uint32_t lo,uint32_t hi){return std::min(std::max(x,lo),hi);});break;
   case 45:ter([](uint32_t x,uint32_t lo,uint32_t hi){return uint32_t(std::min(std::max(s(x),s(lo)),s(hi)));});break;
   case 46:ter([](uint32_t x,uint32_t y,uint32_t t){return u(f(x)*(1-f(t))+f(y)*f(t));});break;
   case 48:bin([](uint32_t e,uint32_t x){return u(f(x)<f(e)?0.f:1.f);});break;
   case 49:ter([](uint32_t e0,uint32_t e1,uint32_t x){float t=(f(x)-f(e0))/(f(e1)-f(e0));t=t<0?0:t>1?1:t;return u(t*t*(3-2*t));});break;
   case 50:ter([](uint32_t x,uint32_t y,uint32_t z){return u(std::fma(f(x),f(y),f(z)));});break;
   case 66:{const Val &x=V(a[0]);float acc=0;for(auto q:x.w)acc+=f(q)*f(q);set(id,rt,{u(std::sqrt(acc))});break;}
   case 67:{const Val &x=V(a[0]),&y=V(a[1]);float acc=0;for(size_t i=0;i<x.w.size();++i){float d=f(x.w[i])-f(y.w[i]);acc+=d*d;}set(id,rt,{u(std::sqrt(acc))});break;}
   case 68:{const Val &x=V(a[0]),&y=V(a[1]);if(x.w.size()!=3)fail("cross");float p=f(x.w[0]),q=f(x.w[1]),r=f(x.w[2]),P=f(y.w[0]),Q=f(y.w[1]),R=f(y.w[2]);set(id,rt,{u(q*R-r*Q),u(r*P-p*R),u(p*Q-q*P)});break;}
   case 69:{const Val &x=V(a[0]);float acc=0;for(auto q:x.w)acc+=f(q)*f(q);float l=std::sqrt(acc);Words o(x.w.size());for(size_t i=0;i<o.size();++i)o[i]=u(f(x.w[i])/l);set(id,rt,std::move(o));break;}
   case 11:un([](uint32_t x){return u(f(x)*0.017453292519943295f);});break;
   default:fail("GLSL.std.450 "+std::to_string(op));
  }
 }
 uint32_t runFunction(uint32_t fn,const std::vector<uint32_t> &args,uint32_t &retVal){
  auto it=funcAt_.find(fn);if(it==funcAt_.end())fail("function body");size_t i=it->second;
  uint32_t param=0;size_t pc=i;uint32_t prev=0,cur=0;retVal=0;
  // parameters
  while(pc<insts()){uint32_t op=w_[at_[pc]]&0xffff;if(op==55){if(param>=args.size())fail("call args");const uint32_t *x=w_+at_[pc];vals_[x[2]]=vals_[args[param]];have_[x[2]]=gen_;++param;++pc;}else break;}
  // pc now at first OpLabel
  for(;;){
   if(pc>=insts())fail("fell off function");const uint32_t *x=w_+at_[pc];uint32_t op=x[0]&0xffff,len=x[0]>>16;
   if(++steps_>200000)fail("step limit");
   switch(op){
    case 248:cur=x[1];++pc;{ // phis first, simultaneously
     std::vector<std::pair<uint32_t,Val>> pend;size_t q=pc;
     while(q<insts() && (w_[at_[q]]&0xffff)==245){const uint32_t *p=w_+at_[q];uint32_t plen=p[0]>>16;bool found=false;for(uint32_t k=3;k+1<plen;k+=2)if(p[k+1]==prev){Val v=V(p[k]);v.type=p[1];pend.push_back({p[2],v});found=true;break;}if(!found)fail("phi predecessor");++q;}
     for(auto &pr:pend){vals_[pr.first]=pr.second;have_[pr.first]=gen_;}pc=q;}
     continue;
    case 245:fail("phi outside block start");
    case 247:case 246:++pc;continue;
    case 249:prev=cur;pc=labelAt_.at(x[1]);continue;
    case 250:prev=cur;{const Val &c=V(x[1]);if(c.w.size()!=1)fail("branch condition");pc=labelAt_.at(c.w[0]?x[2]:x[3]);}continue;
    case 251:{prev=cur;const Val &sel=V(x[1]);uint32_t target=x[2];for(uint32_t k=3;k+1<len;k+=2)if(x[k]==sel.w[0]){target=x[k+1];break;}pc=labelAt_.at(target);continue;}
    case 253:return 0;
    case 254:retVal=x[1];return 1;
    case 252:case 255:fail("kill/unreachable");
    case 57:{std::vector<uint32_t> cargs(x+4,x+len);uint32_t rv=0;uint32_t has=runFunction(x[3],cargs,rv);if(has){vals_[x[2]]=vals_[rv];vals_[x[2]].type=x[1];have_[x[2]]=gen_;}++pc;continue;}
    default:exec(x,op,len);++pc;continue;
   }
  }
 }
 size_t insts() const {return at_.size();}
 void exec(const uint32_t *x,uint32_t op,uint32_t len) {
  switch(op){
   case 59:{uint32_t rt=x[1],id=x[2],sc=x[3];varStorage_[id]=sc;uint32_t pointee=T(rt).elem;varType_[id]=pointee;
    if(sc==7||sc==6||sc==0){ // Function/Private (Output/Input handled at setup)
     Words init(words(pointee),0);if(len>4)init=V(x[4]).w;store_[id]=init;}
    Val v;v.type=rt;v.p.kind=1;v.p.var=id;v.p.type=pointee;vals_[id]=v;have_[id]=gen_;return;}
   case 61:{const Val &p=V(x[3]);Words o=loadPtr(p.p);set(x[2],x[1],std::move(o));return;}
   case 62:{const Val &p=V(x[1]);const Val &v=V(x[2]);if(p.p.kind!=1)fail("store to non-local");auto &st=store_[p.p.var];if(p.p.off+v.w.size()>st.size())fail("local write out of range");std::copy(v.w.begin(),v.w.end(),st.begin()+p.p.off);return;}
   case 65:case 66:{Val r=walk(V(x[3]),x[1],x+4,len-4);vals_[x[2]]=r;vals_[x[2]].type=x[1];have_[x[2]]=gen_;return;}
   case 71:case 72:return;
   case 68:{const Val &p=V(x[3]);if(p.p.kind!=2)fail("array length of non-memory");const Type &st=T(p.p.type);if(st.kind!=tStruct || x[4]>=st.members.size())fail("array length struct");
    const Type &at=T(st.members[x[4]]);auto md=mdec_.find({p.p.type,x[4]});auto d=dec_.find(st.members[x[4]]);uint32_t stride=d==dec_.end()?0:d->second.arrayStride;if(!at.runtime || md==mdec_.end() || !stride)fail("array length layout");
    size_t base=p.p.off+md->second.offset;set(x[2],x[1],{uint32_t(p.p.mem.size>base?(p.p.mem.size-base)/stride:0)});return;} // OpArrayLength: whole elements in the bound range
   case 83:{vals_[x[2]]=V(x[3]);vals_[x[2]].type=x[1];have_[x[2]]=gen_;return;}
   case 80:{Words o;for(uint32_t k=3;k<len;++k){auto &c=V(x[k]);o.insert(o.end(),c.w.begin(),c.w.end());}set(x[2],x[1],std::move(o));return;}
   case 81:{const Val &c=V(x[3]);uint32_t cur=c.type;size_t off=0;for(uint32_t k=4;k<len;++k){const Type &t=T(cur);uint32_t idx=x[k];if(t.kind==tStruct){for(uint32_t m=0;m<idx;++m)off+=words(t.members[m]);cur=t.members[idx];}else if(t.kind==tVec||t.kind==tMat||t.kind==tArr){off+=size_t(idx)*words(t.elem);cur=t.elem;}else fail("extract");}
    size_t n=words(cur);if(off+n>c.w.size())fail("extract range");set(x[2],x[1],Words(c.w.begin()+off,c.w.begin()+off+n));return;}
   case 82:{Val c=V(x[4]);const Val &obj=V(x[3]);uint32_t cur=c.type;size_t off=0;for(uint32_t k=5;k<len;++k){const Type &t=T(cur);uint32_t idx=x[k];if(t.kind==tStruct){for(uint32_t m=0;m<idx;++m)off+=words(t.members[m]);cur=t.members[idx];}else if(t.kind==tVec||t.kind==tMat||t.kind==tArr){off+=size_t(idx)*words(t.elem);cur=t.elem;}else fail("insert");}
    if(off+obj.w.size()>c.w.size())fail("insert range");std::copy(obj.w.begin(),obj.w.end(),c.w.begin()+off);set(x[2],x[1],std::move(c.w));return;}
   case 79:{const Val &a=V(x[3]),&b=V(x[4]);Words o;for(uint32_t k=5;k<len;++k){uint32_t i=x[k];if(i==0xffffffffu)o.push_back(0);else if(i<a.w.size())o.push_back(a.w[i]);else if(i-a.w.size()<b.w.size())o.push_back(b.w[i-a.w.size()]);else fail("shuffle");}set(x[2],x[1],std::move(o));return;}
   case 77:{const Val &a=V(x[3]),&i=V(x[4]);if(i.w[0]>=a.w.size())fail("extract dynamic");set(x[2],x[1],{a.w[i.w[0]]});return;}
   case 78:{Val a=V(x[3]);const Val &c=V(x[4]),&i=V(x[5]);if(i.w[0]>=a.w.size())fail("insert dynamic");a.w[i.w[0]]=c.w[0];set(x[2],x[1],std::move(a.w));return;}
   case 84:{const Val &m=V(x[3]);const Type &mt=T(m.type);uint32_t cols=mt.count,rows=T(mt.elem).count;Words o(m.w.size());for(uint32_t c=0;c<cols;++c)for(uint32_t r=0;r<rows;++r)o[r*cols+c]=m.w[c*rows+r];set(x[2],x[1],std::move(o));return;}
   case 12:{uint32_t extSet=x[3],e=x[4];(void)extSet;ext(x[1],x[2],e,x+5,len-5);return;}
   case 248:case 245:return;
   default:if(arith(op,x[1],x[2],x+3,len-3))return;fail("opcode "+std::to_string(op));
  }
 }
public:
 SpirvEval(const uint32_t *w,size_t n):w_(w),n_(n),bound_(n>3?w[3]:0){}
 VsResult run(const char *entry,const VsInputs &in,const std::vector<uint32_t> &vertexIndices){
  VsResult res;try{
   if(!w_ || n_<5 || w_[0]!=0x07230203 || bound_==0 || bound_>(1u<<20))fail("module");
   in_=&in;steps_=0;store_.clear();priv_.clear();varStorage_.clear();varType_.clear();
   if(vals_.size()!=bound_){vals_.assign(bound_,Val{});have_.assign(bound_,0);gen_=0;}
   if(++gen_==0){std::fill(have_.begin(),have_.end(),0u);gen_=1;} // stamps from earlier runs never count as defined
   // pass 1 (once per entry point; a reused evaluator skips it): decorations, types, entry point, function bodies
   if(!prepared_ || preparedEntry_!=entry){prepared_=false;
   at_.clear();for(size_t i=5;i<n_;){uint32_t len=w_[i]>>16;if(!len || i+len>n_)fail("truncated");at_.push_back(i);i+=len;}
   relaxed_=false;types_.clear();dec_.clear();mdec_.clear();funcAt_.clear();iface_.clear();entryId_=0;
   uint32_t curFn=0;uint32_t &entryId=entryId_;
   for(size_t k=0;k<at_.size();++k){const uint32_t *x=w_+at_[k];uint32_t op=x[0]&0xffff,len=x[0]>>16;
    switch(op){
     case 15:{const char *nm=(const char *)(x+3);size_t nl=strnlen(nm,(len-3)*4);if(nl==strlen(entry) && !memcmp(nm,entry,nl) && x[1]==0){entryId=x[2];iface_.assign(x+3+(nl/4+1),x+len);}break;}
     case 71:{auto &d=dec_[x[1]];uint32_t dd=x[2];if(dd==0)relaxed_=true;else if(dd==30)d.location=x[3];else if(dd==11)d.builtin=x[3];else if(dd==34)d.set=x[3];else if(dd==33)d.binding=x[3];else if(dd==1)d.specId=x[3];else if(dd==6)d.arrayStride=x[3];break;}
     case 72:{auto &d=mdec_[{x[1],x[2]}];uint32_t dd=x[3];if(dd==35)d.offset=x[4];else if(dd==11)d.builtin=x[4];else if(dd==4)d.rowMajor=true;else if(dd==7)d.matrixStride=x[4];break;}
     case 19:types_[x[1]]=Type{tVoid};break;
     case 20:types_[x[1]]=Type{tBool,32};break;
     case 21:{Type t{tInt,x[2],x[3]};types_[x[1]]=t;break;}
     case 22:types_[x[1]]=Type{tFloat,x[2]};break;
     case 23:{Type t{tVec};t.elem=x[2];t.count=x[3];types_[x[1]]=t;break;}
     case 24:{Type t{tMat};t.elem=x[2];t.count=x[3];types_[x[1]]=t;break;}
     case 28:{Type t{tArr};t.elem=x[2];t.count=0;types_[x[1]]=t;break;} // length patched when constants are known
     case 29:{Type t{tArr};t.elem=x[2];t.count=UINT32_MAX;t.runtime=true;types_[x[1]]=t;break;} // OpTypeRuntimeArray: only reachable through memory pointers; reads are range-checked
     case 30:{Type t{tStruct};t.members.assign(x+2,x+len);types_[x[1]]=t;break;}
     case 32:{Type t{tPtr};t.storage=x[2];t.elem=x[3];types_[x[1]]=t;break;}
     case 33:types_[x[1]]=Type{tFunc};break;
     case 54:curFn=x[2];funcAt_[curFn]=k+1;break;
     default:break;
    }}
   (void)curFn;
   // labels
   labelAt_.clear();for(size_t k=0;k<at_.size();++k)if((w_[at_[k]]&0xffff)==248)labelAt_[w_[at_[k]+1]]=k;
   preparedEntry_=entry;prepared_=true;}
   const uint32_t entryId=entryId_;
   if(relaxed_)fail("relaxed precision"); // mediump may run in half ALUs on the GPU; the CPU result would not be bit-comparable
   if(!entryId)fail("entry point");
   // pass 2: constants (global section), in order, once per run
   for(size_t k=0;k<at_.size();++k){const uint32_t *x=w_+at_[k];uint32_t op=x[0]&0xffff,len=x[0]>>16;
    if(op==54)break;
    switch(op){
     case 43:{const Type &t=T(x[1]);if(t.width!=32)fail("wide constant");set(x[2],x[1],{x[3]});break;}
     case 41:set(x[2],x[1],{1});break;
     case 42:set(x[2],x[1],{0});break;
     case 50:{auto d=dec_.find(x[2]);uint32_t v=x[3];if(d!=dec_.end() && d->second.specId!=UINT32_MAX){auto s_=in.spec.find(d->second.specId);if(s_!=in.spec.end())v=s_->second;}set(x[2],x[1],{v});break;}
     case 48:case 49:{auto d=dec_.find(x[2]);uint32_t v=op==48;if(d!=dec_.end() && d->second.specId!=UINT32_MAX){auto s_=in.spec.find(d->second.specId);if(s_!=in.spec.end())v=s_->second!=0;}set(x[2],x[1],{v});break;}
     case 44:case 51:{Words o;for(uint32_t q=3;q<len;++q){auto &c=V(x[q]);o.insert(o.end(),c.w.begin(),c.w.end());}set(x[2],x[1],std::move(o));break;}
     case 46:set(x[2],x[1],Words(words(x[1]),0));break;
     case 1:set(x[2],x[1],Words(words(x[1]),0));break;
     case 52:{if(len<4)fail("spec op");uint32_t sub=x[3];arith(sub,x[1],x[2],x+4,len-4)?void():fail("spec constant op "+std::to_string(sub));break;}
     case 59:{uint32_t sc=T(x[1]).storage;varStorage_[x[2]]=sc;uint32_t pointee=T(x[1]).elem;varType_[x[2]]=pointee;Val v;v.type=x[1];v.p.var=x[2];v.p.type=pointee;
      auto d=dec_.find(x[2]);uint32_t set_=d==dec_.end()?0:d->second.set,bind=d==dec_.end()?0:d->second.binding;
      if(sc==2||sc==12){auto r=in.buffers.find({set_,bind});v.p.kind=2;if(r!=in.buffers.end())v.p.mem=r->second;else v.p.mem=VsInputs::Region{};}
      else if(sc==9){v.p.kind=2;v.p.mem={in.push,in.pushSize};}
      else {v.p.kind=1;store_[x[2]]=Words(words(pointee),0);if(len>4)store_[x[2]]=V(x[4]).w;if(sc==6)priv_[x[2]]=store_[x[2]];}
      vals_[x[2]]=v;have_[x[2]]=gen_;break;}
     case 28:break;
     default:break;
    }
    // array lengths become known once their constant exists
    if(op==28){auto &t=types_[x[1]];auto it=have_[x[3]]==gen_?vals_[x[3]].w:Words{};if(it.empty())fail("array length");t.count=it[0];}
   }
   // fix: arrays declared before constants resolve lazily (declared after their length constant in valid SPIR-V, so the loop above sees both)
   res.position.clear();
   for(uint32_t vi:vertexIndices){
    // reset variable state
    for(auto &kv:varStorage_){uint32_t sc=kv.second;if(sc==1||sc==3)store_[kv.first]=Words(words(varType_[kv.first]),0);else if(sc==6)store_[kv.first]=priv_[kv.first];}
    vertex_=vi;
    // inputs
    for(uint32_t id:iface_){auto sc=varStorage_.find(id);if(sc==varStorage_.end() || sc->second!=1)continue;auto d=dec_.find(id);auto &st=store_[id];
     if(d!=dec_.end() && d->second.builtin!=UINT32_MAX){uint32_t b=d->second.builtin;uint32_t val=b==42||b==5?vi:b==43||b==6?in.instance:b==4424?in.baseVertex:b==4425?0:UINT32_MAX;if(val==UINT32_MAX)fail("input builtin "+std::to_string(b));if(st.size()!=1)fail("builtin width");st[0]=val;}
     else if(d!=dec_.end() && d->second.location!=UINT32_MAX){uint32_t lane[4]{};if(!in.attribute || !in.attribute(d->second.location,vi,lane))fail("vertex attribute "+std::to_string(d->second.location));
      if(st.size()>4)fail("wide attribute");for(size_t q=0;q<st.size();++q)st[q]=lane[q];}
     else fail("input without location");}
    // Private variables have initializers re-applied (already by reset above for Private-like (6)); Function vars init at OpVariable.
    uint32_t rv=0;runFunction(entryId,{},rv);
    // find position
    bool got=false;
    for(uint32_t id:iface_){auto sc=varStorage_.find(id);if(sc==varStorage_.end() || sc->second!=3)continue;auto d=dec_.find(id);auto &st=store_[id];
     if(d!=dec_.end() && d->second.builtin==0){if(st.size()<4)fail("position width");res.position.push_back({f(st[0]),f(st[1]),f(st[2]),f(st[3])});got=true;break;}
     const Type &t=T(varType_[id]);if(t.kind==tStruct){size_t off=0;for(size_t m=0;m<t.members.size();++m){auto md=mdec_.find({varType_[id],uint32_t(m)});if(md!=mdec_.end() && md->second.builtin==0){if(off+4>st.size())fail("position range");res.position.push_back({f(st[off]),f(st[off+1]),f(st[off+2]),f(st[off+3])});got=true;break;}off+=words(t.members[m]);}if(got)break;}}
    if(!got)fail("no gl_Position output");
   }
   res.ok=true;res.steps=steps_;
  }catch(const Fail &e){res.ok=false;res.why=e.why;}catch(const std::out_of_range &){res.ok=false;res.why="bad id";}
  return res;
 }
};
}
