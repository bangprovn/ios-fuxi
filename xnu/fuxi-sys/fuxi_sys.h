/*
 * Copyright (c) 2026 OOSEX GANG (HackiOS). All rights reserved.
 * Copyright (c) 2026 ios-fuxi contributors.
 *
 * @APPLE_OSREFERENCE_LICENSE_HEADER_START@
 *
 * This file contains Original Code and/or Modifications of Original Code
 * as defined in and that are subject to the Apple Public Source License
 * Version 2.0 (the 'License'). You may not use this file except in
 * compliance with the License. The rights granted to you under the License
 * may not be used to create, or enable the creation or redistribution of,
 * unlawful or unlicensed copies of an Apple operating system, or to
 * circumvent, violate, or enable the circumvention or violation of, any
 * terms of an Apple operating system software license agreement.
 *
 * Please obtain a copy of the License at
 * http://www.opensource.apple.com/apsl/ and read it before using this file.
 *
 * The Original Code and all software distributed under the License are
 * distributed on an 'AS IS' basis, WITHOUT WARRANTY OF ANY KIND, EITHER
 * EXPRESS OR IMPLIED, AND APPLE HEREBY DISCLAIMS ALL SUCH WARRANTIES,
 * INCLUDING WITHOUT LIMITATION, ANY WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE, QUIET ENJOYMENT OR NON-INFRINGEMENT.
 * Please see the License for the specific language governing rights and
 * limitations under the License.
 *
 * @APPLE_OSREFERENCE_LICENSE_HEADER_END@
 */
/*
 * Ported from HackiOS hackios-xnu-kernel xnu/files/bsd/kern/kern_minimac.c
 * (APSL 2.0): the privilege-by-entitlement table of its Sandbox stand-in
 * (minisandbox_priv_grant) and its answer to Sandbox request 4 for a process
 * without a container. Adapted to the SWEET policies in sweet_amfi.c, which
 * includes this file only. Both behaviours are off unless their boot-arg is
 * set; see docs-lanes/sys-port.md.
 *
 *   fuxiprivs=1   A privilege that no policy grants is granted to the current
 *                 process when its own code signature carries the entitlement
 *                 iOS pairs with it (dasd "vm-pressure-level", symptomsd
 *                 com.apple.private.network.statistics, ...). Only for a
 *                 question about the caller's own credential. The first grant
 *                 of each privilege is logged.
 *   fuxisbpath=1  Sandbox request 4 for a process without CFFIXED_USER_HOME
 *                 (no container) succeeds with the caller's buffer untouched,
 *                 as on HackiOS, instead of H1's ENOENT. Only while H1's
 *                 container answer itself is enabled (fuxiamfi, default on).
 *   fuxisbext=1   Sandbox requests 5 and 6 (issue and consume an extension)
 *                 succeed. Not from HackiOS, which succeeds without writing the
 *                 token; see fuxi_sys_extension_issue().
 */
#ifndef FUXI_SYS_H
#define FUXI_SYS_H

static TUNABLE(uint32_t, fuxi_sys_privs, "fuxiprivs", 0);
static TUNABLE(uint32_t, fuxi_sys_sbpath, "fuxisbpath", 0);
static TUNABLE(uint32_t, fuxi_sys_sbext, "fuxisbext", 0);

struct fuxi_sys_priv_entitlement {
	int priv;
	const char *entitlement;
};

/* Pairings as iOS 16's own programs carry them (HackiOS minisandbox_privileges). */
static const struct fuxi_sys_priv_entitlement fuxi_sys_priv_entitlements[] = {
	{ PRIV_PROC_UUID_POLICY, "com.apple.private.kernel.uuid-policy" },
	{ PRIV_GLOBAL_PROC_INFO, "com.apple.private.kernel.global-proc-info" },
	{ PRIV_SYSTEM_OVERRIDE, "com.apple.private.kernel.system-override" },
	{ PRIV_HW_DEBUG_DATA, "com.apple.private.kernel.hw-debug-data" },
	{ PRIV_SELECTIVE_FORCED_IDLE, "com.apple.private.kernel.selective-forced-idle" },
	{ PRIV_DARKBOOT, "com.apple.private.kernel.darkboot" },
	{ PRIV_AUDIO_LATENCY, "com.apple.private.kernel.audio_latency" },
	{ PRIV_KTRACE_BACKGROUND, "com.apple.private.kernel.ktrace-background" },
	{ PRIV_SETPRIORITY_DARWIN_ROLE, "com.apple.private.kernel.setpriority-darwin-role" },
	{ PRIV_PACKAGE_EXTENSIONS, "com.apple.private.kernel.set-package-extensions" },
	{ PRIV_TRIM_ACTIVE_FILE, "com.apple.private.apfs.trim-active-file" },
	{ PRIV_PROC_CPUMON_OVERRIDE, "com.apple.private.kernel.override-cpumon" },
	{ PRIV_VM_PRESSURE, "vm-pressure-level" },
	{ PRIV_VM_JETSAM, "com.apple.private.kernel.jetsam" },
	{ PRIV_NET_PRIVILEGED_TRAFFIC_CLASS, "com.apple.private.network.privileged-traffic-class" },
	{ PRIV_NET_PRIVILEGED_SOCKET_DELEGATE, "com.apple.private.network.socket-delegate" },
	{ PRIV_NET_INTERFACE_CONTROL, "com.apple.private.network.interface-control" },
	{ PRIV_NET_PRIVILEGED_NETWORK_STATISTICS, "com.apple.private.network.statistics" },
	{ PRIV_NET_PRIVILEGED_NECP_POLICIES, "com.apple.private.necp.policies" },
	{ PRIV_NET_RESTRICTED_AWDL, "com.apple.private.network.awdl.restricted" },
	{ PRIV_NET_QOSMARKING_POLICY_OVERRIDE, "com.apple.private.network.qos-marking-policy-override" },
	{ PRIV_NET_RESTRICTED_MULTIPATH_EXTENDED, "com.apple.developer.networking.multipath_extended" },
	{ PRIV_NET_RESTRICTED_ROUTE_NC_READ, "com.apple.developer.networking.route_nc_read" },
	{ PRIV_NET_VALIDATED_RESOLVER, "com.apple.private.validated-resolver" },
	{ PRIV_NET_CUSTOM_PROTOCOL, "com.apple.developer.networking.custom-protocol" },
	{ PRIV_NET_PRIVILEGED_NECP_DROP_ALL_BYPASS, "com.apple.private.necp.drop_all_bypass" },
	{ PRIV_NET_PRIVILEGED_IPSEC_WAKE_PACKET, "com.apple.private.network.ipsec-wake-packet" },
	{ PRIV_NETINET_RESERVEDPORT, "com.apple.private.network.reserved-port" },
	{ PRIV_NETINET_TCP_KA_OFFLOAD, "com.apple.developer.networking.tcp_ka_offload" },
	{ PRIV_SKYWALK_REGISTER_USER_PIPE, "com.apple.private.skywalk.register-user-pipe" },
	{ PRIV_SKYWALK_REGISTER_KERNEL_PIPE, "com.apple.private.skywalk.register-kernel-pipe" },
	{ PRIV_SKYWALK_REGISTER_NET_IF, "com.apple.private.skywalk.register-net-if" },
	{ PRIV_SKYWALK_REGISTER_FLOW_SWITCH, "com.apple.private.skywalk.register-flow-switch" },
	{ PRIV_SKYWALK_OBSERVE_ALL, "com.apple.private.skywalk.observe-all" },
	{ PRIV_SKYWALK_OBSERVE_STATS, "com.apple.private.skywalk.observe-stats" },
	{ PRIV_VFS_OPEN_BY_ID, "com.apple.private.vfs.open-by-id" },
	{ PRIV_VFS_MOVE_DATA_EXTENTS, "com.apple.private.vfs.move-data-extents" },
	{ PRIV_VFS_SNAPSHOT, "com.apple.private.vfs.snapshot" },
	{ PRIV_VFS_DATALESS_RESOLVER, "com.apple.private.vfs.dataless-resolver" },
	{ PRIV_VFS_SETSIZE, "com.apple.private.vfs.setsize" },
};
#define FUXI_SYS_PRIVS \
	(sizeof(fuxi_sys_priv_entitlements) / sizeof(fuxi_sys_priv_entitlements[0]))
_Static_assert(FUXI_SYS_PRIVS <= 64, "one log bit per table entry");

/* Bit i set: the grant of table entry i has been logged (once per boot). */
static uint64_t fuxi_sys_priv_logged;

/* The table entry of a privilege, or -1. */
static int
fuxi_sys_priv_index(int priv)
{
	for (unsigned i = 0; i < FUXI_SYS_PRIVS; i++) {
		if (fuxi_sys_priv_entitlements[i].priv == priv) {
			return (int)i;
		}
	}
	return -1;
}

/*
 * 0: granted by entitlement; EPERM: not this policy's to grant (the caller
 * then logs and refuses as before). The entitlements are the current
 * process's, so only a question about its own credential is answered.
 */
static int
fuxi_sys_priv_grant(kauth_cred_t cred, int priv)
{
	int index;
	proc_t proc;

	if (!fuxi_sys_privs || cred != kauth_cred_get()) {
		return EPERM;
	}
	index = fuxi_sys_priv_index(priv);
	if (index < 0) {
		return EPERM;
	}
	proc = current_proc();
	if (sweet_amfi_query_boolean_with_proc(proc,
	    fuxi_sys_priv_entitlements[index].entitlement) != KERN_SUCCESS) {
		return EPERM;
	}
	if (!(os_atomic_or_orig(&fuxi_sys_priv_logged, 1ULL << index, relaxed) & (1ULL << index))) {
		char name[32];
		proc_selfname(name, sizeof(name));
		printf("FUXI SYS: %s privilege %d granted by %s\n", name, priv,
		    fuxi_sys_priv_entitlements[index].entitlement);
	}
	return 0;
}

/*
 * The result of Sandbox request 4 from H1's container answer: with
 * fuxisbpath=1 "no container" (ENOENT) becomes success without a write.
 * Every other error (ESRCH, ENAMETOOLONG, copy faults) is kept.
 */
static int
fuxi_sys_container_result(int error)
{
	return error == ENOENT && fuxi_sys_sbpath ? 0 : error;
}

/*
 * Sandbox extensions. There is no sandbox here for a token to open, but iOS
 * treats a failure to get one as a refusal: tccd grants Safari the microphone
 * by entitlement and then "Failed to issue Sandbox Extension for
 * kTCCServiceMicrophone" comes back to Safari as kTCCErrorDomain 6, so
 * getUserMedia is denied with no prompt (a-v8b). The same line stands in front
 * of Photos and Contacts, and of mobileassetd, cloudd, linkd and nsurlsessiond
 * handing files to their clients.
 *
 * The arguments are as libsystem_sandbox 20H392 passes them. Its common issue
 * path mallocs 2048 bytes for the token and returns them when the call
 * succeeds; consume takes the token and its size with the terminator, and -1 is
 * its own failure value, so handles start at 1.
 */
struct fuxi_sys_extension_issue_args {
	user_addr_t name;       /* the extension's class */
	uint64_t type;
	user_addr_t data;       /* path, service name, or nothing */
	uint64_t flags;
	user_addr_t token;
	int64_t target;
	uint64_t target_version;
};

struct fuxi_sys_extension_consume_args {
	user_addr_t token;
	uint64_t size;
	user_addr_t handle;
};

static uint64_t fuxi_sys_extension_handle;

static int
fuxi_sys_extension_issue(user_addr_t arg)
{
	struct fuxi_sys_extension_issue_args args;
	char name[128];
	char token[sizeof(name) + 32];
	size_t done = 0;
	int error = copyin(arg, &args, sizeof(args));
	if (error) {
		return error;
	}
	error = copyinstr(args.name, name, sizeof(name), &done);
	if (error) {
		return error;
	}
	/* Nobody reads a token but this file, and this file does not: it only has to be a string. */
	int length = snprintf(token, sizeof(token), "fuxi;%llx;%s", (unsigned long long)args.type, name);
	return copyout(token, args.token, (size_t)length + 1);
}

static int
fuxi_sys_extension_consume(user_addr_t arg)
{
	struct fuxi_sys_extension_consume_args args;
	int error = copyin(arg, &args, sizeof(args));
	if (error) {
		return error;
	}
	uint64_t handle = os_atomic_inc(&fuxi_sys_extension_handle, relaxed);
	return copyout(&handle, args.handle, sizeof(handle));
}

static void
fuxi_sys_report(void)
{
	if (fuxi_sys_privs || fuxi_sys_sbpath || fuxi_sys_sbext) {
		printf("FUXI SYS: privileges by entitlement %s (%u pairs), uncontained container path %s, extensions %s\n",
		    fuxi_sys_privs ? "on" : "off", (unsigned)FUXI_SYS_PRIVS,
		    fuxi_sys_sbpath ? "succeeds untouched" : "ENOENT",
		    fuxi_sys_sbext ? "issued" : "ENOENT");
	}
}

#endif /* FUXI_SYS_H */
