#include <string>
#include <initializer_list>
#define FUXI_MTL_HOST_TEST
#include "TranslationPolicy.hpp"
#include <cassert>
int main(int argc,char **argv){
 if(argc==5 && !strcmp(argv[1],"half")){auto p=fuxi::systemTranslationPolicy();assert(p.halfRound==unsigned(atoi(argv[2])) && fuxi::halfRoundLossy(p,uint32_t(strtoul(argv[3],nullptr,16)))==(argv[4][0]=='1'));return 0;} // FUXI_MTL_HALF_ROUND(_PIPES)
 if(argc==3 && !strcmp(argv[1],"lean")){assert(fuxi::systemTranslationPolicy().submitLean==(argv[2][0]=='1'));return 0;} // FUXI_MTL_SUBMIT_LEAN: exactly "1" enables
 if(argc==3 && !strcmp(argv[1],"rename")){assert(fuxi::systemTranslationPolicy().shadowRename==(argv[2][0]=='1'));return 0;}
 if(argc==2){auto p=fuxi::systemTranslationPolicy();assert(p.stages==(argv[1][0]=='1') && p.trace==(argv[1][1]=='1') && p.async==(argv[1][2]=='1') && p.compileBoost==(argv[1][3]=='1'));if(strlen(argv[1])==6)assert(p.frameTrace==(argv[1][4]=='1') && p.pipelined==(argv[1][5]=='1'));return 0;}
 using namespace fuxi;
 static_assert(sizeof("fuxigpuperf")-1<=16);
 assert(!translationPolicy(nullptr).perf);
 auto early=translationPolicy("fuxigpuoverlap=1 fuxigpusubmit=1");assert(earlyScheduledWait(early,true,true,"backboardd"));
 assert(!earlyScheduledWait(early,true,false,"backboardd"));
 assert(!earlyScheduledWait(early,false,true,"backboardd") && !earlyScheduledWait(early,true,true,"SpringBoard") && !earlyScheduledWait(early,true,true,nullptr));
 for(auto args:{"", "fuxigpuoverlap=1", "fuxigpusubmit=1", "fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpusubmit=1"})assert(!earlyScheduledWait(translationPolicy(args),true,true,"backboardd"));
 for(unsigned i=0;i<8;++i)assert(translationPolicy(("fuxigpuperf="+std::to_string(i)).c_str()).perf==i);
 for(auto value:{"8","9","255","-1","01","1x","","3 fuxigpuperf=1","3 fuxigpuperf=bad"})assert(!translationPolicy((std::string("fuxigpuperf=")+value).c_str()).perf);
 assert(translationPolicy("other=1\tfuxigpuperf=3\n").perf==3 && !translationPolicy("prefixfuxigpuperf=3").perf);
 static_assert(sizeof("fuxigpuoverlap")-1<=16 && sizeof("fuxigpusubmit")-1<=16);
 assert(!translationPolicy(nullptr).overlap && !translationPolicy(nullptr).submitFast);
 assert(translationPolicy("fuxigpuoverlap=1 fuxigpusubmit=1").overlap && translationPolicy("fuxigpuoverlap=1 fuxigpusubmit=1").submitFast);
 assert(!translationPolicy(nullptr).skipSampling && translationPolicy("fuxigpuskip=1").skipSampling);
 assert(!translationPolicy(nullptr).merge && translationPolicy("fuxigpumerge=1").merge);
 assert(!translationPolicy(nullptr).gmem && translationPolicy("fuxigpugmem=1").gmem && !translationPolicy("fuxigpugmem=1").allowFeedback);
 assert(!translationPolicy(nullptr).allowFeedback && translationPolicy("fuxigpufeedback=1").allowFeedback && !translationPolicy("fuxigpufeedback=1").skipSampling);
 for(unsigned level=0;level<=2;++level){auto p=translationPolicy(("fuxigpuframetrace="+std::to_string(level)).c_str());assert(p.frameTrace==(level!=0) && p.frameCPU==(level==2));}
 for(auto suffix:{"3","-1","02","2x","","2 fuxigpuframetrace=1","1 fuxigpuframetrace=2","2 fuxigpuframetrace=2"}){auto p=translationPolicy((std::string("fuxigpuframetrace=")+suffix).c_str());assert(!p.frameTrace && !p.frameCPU);}
 assert(!translationPolicy(nullptr).frameTrace && !translationPolicy(nullptr).pipelined);
 assert(translationPolicy("fuxigpuframetrace=1").frameTrace && !translationPolicy("fuxigpuframetrace=1").pipelined);
 assert(translationPolicy("fuxigpupipelined=1").pipelined && !translationPolicy("fuxigpupipelined=1").frameTrace);
 assert(!translationPolicy(nullptr).stages && !translationPolicy("").trace && !translationPolicy(nullptr).compileBoost);
 auto p=translationPolicy("fuxigpustages=1 fuxigputrace=1 fuxigpuasync=1 fuxicompileboost=1");assert(p.stages && p.trace && p.async && p.compileBoost);
 for(auto key:{"fuxigpustages","fuxigputrace","fuxigpuasync","fuxicompileboost","fuxigpuframetrace","fuxigpupipelined","fuxigpuskip","fuxigpufeedback","fuxigpugmem","fuxigpuoverlap","fuxigpusubmit","fuxigpumerge"}){
  auto check=[&](const std::string &s){return translationOption(s.c_str(),(std::string(key)+"=").c_str());};
  assert(check(std::string("other=1\t")+key+"=1\r\n"));
  for(auto suffix:{"0","2","","11","yes"})assert(!check(std::string(key)+"="+suffix));
  assert(!check(std::string(key)+"=1 "+key+"=1"));assert(!check(std::string(key)+"=1 "+key+"=0"));
  assert(!check(std::string("prefix")+key+"=1"));
 }
 puts("PASS: translation defaults, exact tokens, duplicate/malformed settings fail closed");
}
