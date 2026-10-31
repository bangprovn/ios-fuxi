#!/usr/bin/env python3
"""The ath12k hand-off in production fuxi_wlan.c (FUXI_ATH12K), against the W15 fake chip and WLFW peer.

A stand-in for the object plays ath12k: it receives the CAP data at FW_READY, takes its DMA pool, starts the
firmware with the same tables W15 sends (the peer checks every byte), and stops it again. Then the real
containment runs. Also: the recipe defaults, ath12k's own tables on the wire, the CAL_DONE upload size,
the stages and the station address, and that nothing changes without fuxiath12k. No object from
build-ath12k.py is involved.
"""
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('w15', ROOT / 'tools/test-fuxi-wlan-w15.py')
w15 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w15)

PREAMBLE_HEAD = f'''#define FUXI_ATH12K 1
#include <stdint.h>
#include "{ROOT / 'xnu/fuxi-ath12k/src/fa_xnu.h'}"
'''
PREAMBLE_TAIL = r'''
static uint8_t fake_bar[0x100000];
static uintptr_t fw_bar_window(void) { return (uintptr_t)fake_bar; }
static unsigned mhi_locks, mhi_depth, fa_stops, fa_entries;
void fuxi_ath12k_mhi_lock(void) { assert(!mhi_depth); mhi_depth++; mhi_locks++; }
void fuxi_ath12k_mhi_unlock(void) { assert(mhi_depth==1); mhi_depth--; }
'''

PEER = w15.PEER.replace('    if(msg==0x2f || msg==0x23 || msg==0x22) {', r'''
    if(msg==0x22 && seen15==3) {
        /* ath12k_qmi_firmware_stop: mandatory 0x01 u32 mode OFF (4), optional 0x10 hw_debug 0. */
        assert(len==7+7+4 && !memcmp(q+7,(uint8_t[]){1,4,0,4,0,0,0,0x10,1,0,0},11));
        mode_offs++;
        reply(txn,msg,false);
        return;
    }
    if(msg==0x2f || msg==0x23 || msg==0x22) {''')
PEER = PEER.replace('static unsigned trial, responses,', 'static unsigned mode_offs;\nstatic unsigned trial, responses,', 1)

# The phone's CMEM (w-p20): dp.c:1304 uses it as a base, so the CAP reply carries it here.
assert '    if(io_case==21)p[25]=0x71; /* absent board */' in PEER
PEER = PEER.replace('    if(io_case==21)p[25]=0x71; /* absent board */', r'''
    p[n++]=0x1c;put16(p+n,64);n+=2;memset(p+n,0,64);
    put32(p+n,0x113000);put32(p+n+8,0x5000);n+=64;
    if(io_case==21)p[25]=0x71; /* absent board */''')

TESTS = PEER + r'''
static void boot15(unsigned c,bool keep) {
    io_case=0;trial=0;responses=dl[0]=dl[1]=m3_requests=0;
    start_case=c;seen15=observe_ticks=late_indications=0;mode_ticks=0;
    begin(0,keep);assert(wl_w15 && wl_w14 && wl_w13 && wl_w12 && wl_qrtr);
    target_cursor=wl_iova_next;
}
static uint32_t golden_ce[9][6];
static unsigned fa_case, fa_stage_wanted=1;
static int fa_result;
static unsigned char seen_mac[6];
static unsigned macs_set;
static struct fa_caps seen_caps;
static void *pool;
static unsigned long long pool_iova;

static struct fa_wlan_cfg stock_cfg(void) {
    for(unsigned i=0;i<9;i++)memcpy(golden_ce[i],wl15_target_ce[i],20);
    return (struct fa_wlan_cfg){golden_ce,9,wl15_service_ce,14,wl15_shadow_v3,36};
}

/* Stands in for ath12k in fa_hif.c, in the order of design section 2. */
int fa_firmware_ready(const struct fa_caps *caps, unsigned stage) {
    fa_entries++;
    assert(stage==fa_stage_wanted && !mhi_depth);
    assert(macs_set==1); /* design section 4: known before anything of ath12k runs */
    seen_caps=*caps;
    assert(wl14_ready && !seen15);
    unsigned long length=0;
    assert(fuxi_ath12k_bar(&length)==(void *)fake_bar && length==0x100000);
    setreg(WL_BAR0+0x20000,0x1234);
    assert(fuxi_ath12k_read32(0x20000)==0x1234);
    fuxi_ath12k_write32(0x20004,0x5678);assert(wl_prd(WL_BAR0+0x20004)==0x5678);
    pool=lx_os_radio_memory(1u<<20,&pool_iova);
    assert(pool && pool_iova>=wl_iova_base && pool_iova<wl_iova_end && wl_fa_pool.nc);
    for(unsigned long off=0;off<(1u<<20);off+=4096)
        assert(walk(pool_iova+off)==(wl_phys((uintptr_t)pool+off)|WL_DMA_PTE));
    unsigned long long again=0;assert(!lx_os_radio_memory(4096,&again));
    if(fa_case==1)return -5;
    if(fa_case==3){setreg(WL_BAR0+wl_bhi+0x28,3);return -5;}
    struct fa_wlan_cfg cfg=stock_cfg();
    /* Refusals never reach the wire. */
    unsigned before=seen15;
    assert(fuxi_ath12k_firmware_start(&cfg,1)==-22 && seen15==before);
    cfg.shadow_count=61;assert(fuxi_ath12k_firmware_start(&cfg,0)==-22);cfg.shadow_count=36;
    assert(!strstr(logbuf,"match-table"));
    int e=fuxi_ath12k_firmware_start(&cfg,0);
    if(fa_case==2){assert(e==-5);return e;}
    assert(!e && seen15==3 && strstr(logbuf,"FUXI ATH12K: cfg ce 9 svc 14 shadow 36 match-table 1"));
    /* MODE to CE init: no 2 s observation in between. */
    assert(!strstr(logbuf,"observing indications"));
    unsigned locks=mhi_locks;
    fuxi_ath12k_mhi_service();
    for(unsigned i=0;i<20;i++){wl_sleep(1);fuxi_ath12k_mhi_service();}
    assert(mhi_locks>locks && !wl_w11_error);
    fuxi_ath12k_firmware_stop();
    assert(mode_offs==1 && strstr(logbuf,"FUXI ATH12K: mode off ok"));
    return fa_result;
}
void fa_stop(void) { fa_stops++; assert(!mhi_depth); }
void fa_set_radio_mac(const unsigned char *mac) { memcpy(seen_mac,mac,6); macs_set++; }

static void boot_fa(unsigned c,bool keep) {
    fa_case=c;fa_entries=fa_stops=mode_offs=macs_set=0;fa_result=0;pool=NULL;
    memset(&seen_caps,0,sizeof(seen_caps));
    /* The peer speaks W15 as configured by the test's own arguments, not the recipe, whose values have
     * their own table test below: the stage turns on after configuration, before the conversation. */
    wl_fa_stage=0;boot15(0,keep);wl_fa_stage=fa_stage_wanted;
}
/* Like the W11 suite's contain(), except that the pool is never given back. */
static void contain_fa(void) {
    wl5_abort(28);
    assert(frees==allocations-1 && !wl_ipcr.va && !wl_contexts.va && wl_fa_pool.va);
    wl_dma_free(&wl_fa_pool);wl_fa_started=false;dev_on=false;
}
static const char *text_arg(const char *name,char *out,int size) {
    return wl_fa_recipe_arg(name,out,size)?out:NULL;
}
int main(void) {
    /* Off by default: every recipe answer is no, and the W15 run stays as it was. */
    char buf[96];int number=0;
    wl_fa_stage=0;
    assert(!wl_fa_recipe_arg("fuxiwlan",&number,sizeof(number)));
    assert(!text_arg("fuxiwlanseq",buf,sizeof(buf)));
    wl_fa_configure();assert(!wl_fa_stage);
    boot15(0,false);{int e=finish_step();assert(!e && wl_w11_ok && seen15==3);}
    assert(strstr(logbuf,"W15 observing indications 2000 ms") && !strstr(logbuf,"FUXI ATH12K"));
    wl_finish(28);dev_on=false;

    /* fuxiath12k>0 alone is the recipe of design section 5, argument by argument. */
    ath12k_arg=1;wl_fa_configure();assert(wl_fa_stage==1);
    assert(strstr(logbuf,"fuxiath12k=1, recipe defaults on, running stage 1; stage 2 is the last one built"));
    ath12k_arg=2;wl_fa_configure();assert(wl_fa_stage==2);
    assert(strstr(logbuf,"fuxiath12k=2, recipe defaults on, running stage 2; stage 2 is the last one built"));
    /* Above the last one built: runs that one, and says so. */
    ath12k_arg=7;wl_fa_configure();assert(wl_fa_stage==2);
    assert(strstr(logbuf,"fuxiath12k=7, recipe defaults on, running stage 2; stage 2 is the last one built"));
    const struct {const char *name;int v;} numbers[]={{"fuxiwlan",1},{"fuxiwlanclk",1},{"fuxiwlanqrtr",5},
        {"fuxiwlanmlo",0x1c1},{"fuxiwlanp18",6},{"fuxiwlankeep",1}};
    for(unsigned i=0;i<6;i++){assert(wl_fa_recipe_arg(numbers[i].name,&number,sizeof(number)) && number==numbers[i].v);}
    assert(!strcmp(text_arg("fuxiwlanprobe",buf,16),"civac"));
    assert(!strcmp(text_arg("fuxiwlanbdf",buf,65),"bd_m3.elf"));
    assert(!strcmp(text_arg("fuxiwlanseq",buf,96),"cycle,parf,dbi,eq,hostirq,msi,por,prefix"));
    assert(!text_arg("fuxiwlanseq",buf,8)); /* never truncated */
    assert(!wl_fa_recipe_arg("fuxiwlanrails",&number,sizeof(number)));
    assert(!wl_fa_recipe_arg("fuxiwlanoldiova",&number,sizeof(number)));
    assert(!wl_fa_recipe_arg("fuxiwlan",buf,sizeof(buf))); /* a number into a wrong-sized buffer */
    /* A present argument wins: the wrappers ask the kernel first. */
    keep_arg=0;number=9;assert(wl_fa_argn("fuxiwlankeep",&number,sizeof(number)) && number==0);
    /* The real seq parser takes the recipe text with no invalid flag. */
    char seq[96];assert(text_arg("fuxiwlanseq",seq,sizeof(seq)));

    /* The stage: handed over at FW_READY with the CAP data, stopped, contained even with keep. */
    for(unsigned keep=0;keep<2;keep++) {
        boot_fa(0,keep);
        int e=finish_step();if(e)fprintf(stderr,"%s",logbuf);
        assert(!e && fa_entries==1 && !wl_keep && wl_w11_ok);
        assert(strstr(logbuf,"FUXI ATH12K: hand-off stage 1 chip 0x2 family 0x4"));
        assert(strstr(logbuf,"FUXI ATH12K: stage 1 done; containment follows"));
        assert(seen_caps.chip_id==2 && seen_caps.chip_family==4);
        assert(seen_caps.dev_mem[0].start==0x113000 && seen_caps.dev_mem[0].size==0x5000 && !seen_caps.dev_mem[1].size);
        assert(strstr(logbuf,"cmem 0x113000/0x5000"));
        assert(seen_caps.fw_build_id[0]);
        unsigned stops=fa_stops;assert(stops==1);
        unsigned before=frees,allocated=allocations;
        wl_finish(28);
        /* wl_keep was cleared, so this contained; fa_stop came first; the pool stays. */
        assert(fa_stops==stops+1 && frees>before);
        assert(wl_fa_pool.va && frees==allocated-1);
        assert(strstr(logbuf,"abort buffers freed"));
        wl_dma_free(&wl_fa_pool);wl_fa_started=false;dev_on=false;
    }
    /* Stage 2 goes the same way in this file: fa_hif.c and core.c are what stop it elsewhere. */
    fa_stage_wanted=2;
    boot_fa(0,true);
    {
        int e=finish_step();assert(!e && fa_entries==1 && !wl_keep && wl_w11_ok && fa_stops==1);
        assert(strstr(logbuf,"FUXI ATH12K: hand-off stage 2 chip 0x2"));
        assert(strstr(logbuf,"FUXI ATH12K: stage 2 done; containment follows"));
    }
    contain_fa();
    fa_stage_wanted=1;
    /* The address: the default, a good one, and each kind that is refused. */
    {
        static const unsigned char fallback[6]={0x02,0x46,0x55,0x58,0x49,0x01};
        const char *const refused[]={"","02:46:55:58:49","02:46:55:58:49:0","02-46-55-58-49-01","02:46:55:58:49:0g",
            "02:46:55:58:49:011","00:00:00:00:00:00","01:00:5e:00:00:01","ff:ff:ff:ff:ff:ff"};
        macs_set=0;mac_arg=NULL;loglen=0;logbuf[0]=0;
        wl_fa_mac_configure();
        assert(macs_set==1 && !memcmp(seen_mac,fallback,6) && strstr(logbuf,"FUXI ATH12K: station address from the default"));
        static const unsigned char given[6]={0x0a,0xbc,0xde,0xf0,0x12,0x34};
        mac_arg="0a:BC:de:F0:12:34";
        wl_fa_mac_configure();
        assert(macs_set==2 && !memcmp(seen_mac,given,6) && strstr(logbuf,"FUXI ATH12K: station address from fuxiwifimac"));
        for(unsigned i=0;i<sizeof(refused)/sizeof(refused[0]);i++){
            mac_arg=refused[i];loglen=0;logbuf[0]=0;
            wl_fa_mac_configure();
            assert(!memcmp(seen_mac,fallback,6) && strstr(logbuf,"FUXI ATH12K: fuxiwifimac refused; default address"));
        }
        mac_arg=NULL;
    }
    /* A board-less CAP becomes ath12k's default. */
    boot_fa(0,true);io_case=21;{int e=finish_step();assert(!e && !wl14_board_valid && seen_caps.board_id==0xff);}
    contain_fa();
    /* ath12k failing before and at firmware start: W11 failure, then containment. */
    boot_fa(1,true);assert(finish_step() && fa_entries==1 && seen15==0 && fa_stops==1 && !wl_keep);
    assert(strstr(logbuf,"FUXI ATH12K: hand-off failed -5; contain"));
    contain_fa();
    /* A firmware already in RDDM is read as it is, and not asked again. */
    boot_fa(3,true);assert(finish_step());
    assert(strstr(logbuf,"FUXI WLAN: ATH12K hand-off diagnostic BHI_EXECENV 0x3") && !strstr(logbuf,"P18 before SYS_ERR"));
    setreg(WL_BAR0+wl_bhi+0x28,2);contain_fa();
    /* A pool that cannot be mapped is an answer of NULL, not a stop of the thread. */
    fail_nth_alloc=0;

    /* ath12k's tables reach the wire as given, with match-table 0. */
    wl_fa_stage=0;boot15(0,true);wl_fa_stage=1;
    wl12_stage=WL15_CFG;
    {
        static uint32_t shadow[37];
        struct fa_wlan_cfg cfg=stock_cfg();
        memcpy(shadow,wl15_shadow_v3,sizeof(wl15_shadow_v3));shadow[36]=0xabcdef;
        cfg.shadow=shadow;cfg.shadow_count=37;golden_ce[8][0]=9;
        for(unsigned i=0;i<9;i++)memcpy(wl_fa_target_ce[i],golden_ce[i],20);
        wl_fa_cfg=cfg;
        static struct wl12_encoder e;bzero(&e,sizeof(e));e.len=7;
        assert(!wl15_encode_cfg(&e));
        /* WIN, nine five-word pipes, fourteen three-word services, 37 addresses. */
        unsigned at=7+6;
        assert(e.data[at]==0x11 && e.data[at+3]==9 && e.data[at+4+8*20]==9);
        at+=4+9*20;assert(e.data[at]==0x12 && e.data[at+3]==14);
        at+=4+14*12;assert(e.data[at]==0x17 && e.data[at+3]==37);
        assert(wl_get32(e.data+at+4+36*4)==0xabcdef && e.len==at+4+37*4);
    }
    contain();

    /* Independent of the stage: the CAL_DONE indication's TLV 0x10 is the upload size. */
    {
        uint8_t p[]={4,0,0,0x3e,0,11,0,0x10,8,0,0x88,0x77,0x66,0x55,0,0,0,0};
        wl12_stage=WL15_OBSERVE;wl18_cal_done=false;
        assert(!wl_qmi_w12(p,sizeof(p)) && wl18_cal_done);
        assert(strstr(logbuf,"FUXI WLAN: P18 CAL_DONE cal file upload size 1432778632"));
        uint8_t q[]={4,0,0,0x3e,0,7,0,0x10,4,0,1,2,3,4};
        assert(!wl_qmi_w12(q,sizeof(q)) && strstr(logbuf,"msg 0x3e unknown TLV 0x10 bytes 4"));
    }
    puts("ATH12K hand-off: recipe defaults, stages 1 and 2 and above, station address, CAP data, pool map, exact INI/CFG/MODE, ath12k tables, mode off, fa_stop before containment, RDDM on failure, pool kept, off by default, CAL_DONE size");
    return 0;
}
'''


def main():
    m6 = w15.w14.w12.w11.w10.m6o.m6
    preamble = m6.PREAMBLE
    preamble = preamble.replace('return 0x12345; }', 'return (uint64_t)sleeps*1000000ull; }')
    assert 'static bool PE_parse_boot_argn(const char *name, void *out, int size) {' in preamble
    preamble = preamble.replace('static bool PE_parse_boot_argn(const char *name, void *out, int size) {',
                                'static int ath12k_arg;\nstatic const char *mac_arg;\nstatic bool PE_parse_boot_argn(const char *name, void *out, int size) {\n'
                                '    if(!strcmp(name,"fuxiwifimac")){if(!mac_arg)return false;strlcpy(out,mac_arg,size);return true;}\n'
                                '    if(!strcmp(name,"fuxiath12k")){if(!ath12k_arg)return false;memcpy(out,&ath12k_arg,size);return true;}')
    m6.PREAMBLE = PREAMBLE_HEAD + preamble + PREAMBLE_TAIL
    w15.w14.TESTS = TESTS
    w15.w14.main()


if __name__ == '__main__':
    main()
