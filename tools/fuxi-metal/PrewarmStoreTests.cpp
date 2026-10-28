#include "PrewarmStore.hpp"
#include <cstdio>
#include <memory>
using namespace fuxi;
#define CHECK(c) do{if(!(c)){fprintf(stderr,"FAIL %s:%d %s\n",__FILE__,__LINE__,#c);return 1;}}while(0)
int main() {
 uint8_t a[37],b[37];for(int i=0;i<37;++i)a[i]=b[i]=uint8_t(i*7);b[36]^=1;
 CHECK(libraryHash(a,37)==libraryHash(a,37));CHECK(!(libraryHash(a,37)==libraryHash(b,37)));CHECK(!(libraryHash(a,36)==libraryHash(a,37)));
 uint8_t id[32];for(int i=0;i<32;++i)id[i]=uint8_t(i);auto k=recipeKey(id);CHECK(k.size()==32 && isRecipeKey((const uint8_t *)k.data()) && !isRecipeKey(id));
 auto o=std::make_unique<CompileOptions>();o->constantCount[0]=2;o->constantCount[1]=1;
 uint8_t *raw=(uint8_t *)&o->constants[0][1];raw[0]=0x5a;
 std::vector<uint8_t> packed;packOptions(*o,packed);CHECK(packed.size()<sizeof(CompileOptions)/4);
 auto u=std::make_unique<CompileOptions>();CHECK(unpackOptions(packed.data(),packed.size(),*u));
 CHECK(u->constantCount[0]==2 && u->constantCount[1]==1 && ((uint8_t *)&u->constants[0][1])[0]==0x5a);
 std::vector<uint8_t> again;packOptions(*u,again);CHECK(again==packed);
 CHECK(!unpackOptions(packed.data(),packed.size()-1,*u));
 Recipe r;r.vertex=libraryHash(a,37);r.fragment=libraryHash(b,37);r.vertexName="vmain";r.fragmentName="fmain";r.options=packed;
 auto blob=encodeRecipe(r);Recipe d;CHECK(decodeRecipe(blob.data(),blob.size(),d));
 CHECK(d.vertex==r.vertex && d.fragment==r.fragment && d.vertexName=="vmain" && d.fragmentName=="fmain" && d.options==packed);
 blob.push_back(0);CHECK(!decodeRecipe(blob.data(),blob.size(),d));blob.pop_back();blob[0]='X';CHECK(!decodeRecipe(blob.data(),blob.size(),d));
 puts("PrewarmStoreTests ok");return 0;
}
