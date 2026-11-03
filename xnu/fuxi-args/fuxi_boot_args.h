/*
 * Fuxi: boot-args from the device tree (patch 0122, xnu/fuxi-args).
 *
 * iBoot's boot line is BOOT_LINE_LENGTH (608) bytes, and the Xiaomi 13's daily line had filled it. The host can now
 * put more boot-args into the ADT as /chosen fuxi-args: one NUL-terminated string of printable ASCII, words split by
 * spaces (boot/add-args-adt.py). PE_init_platform takes it right after SecureDTInit, before pe_identify_machine and
 * before anything reads a boot-arg: nothing can earlier, as PE_boot_args() needs PE_state.bootArgs, set just before,
 * and the TUNABLEs are parsed later, in kernel_startup_bootstrap. From then on PE_parse_boot_argn and
 * PE_parse_boot_arg_str scan the real line, one space, then the property, so its words count as if appended to the
 * line. A name in both: a name=value is the property's (the parser keeps the last), a -flag or a _name the line's
 * (the parser stops at the first).
 *
 * PE_boot_args() stays the real line: kern.bootargs, the panic log and stackshots show it alone. A boot-arg that user
 * space reads (launchd, FuxiMetal, libfuxi-sb, through kern.bootargs) or a host tool reads from the payload must stay
 * on the line. A property that is not a clean string is not taken at all, rather than cut inside a word.
 *
 * Plain C and no includes, so tools/test-fuxi-args.py builds it on the host.
 */
#ifndef FUXI_BOOT_ARGS_H
#define FUXI_BOOT_ARGS_H

#define FUXI_ARGS_MAX   2048u   /* the property's largest size, its NUL included */

enum {
	FUXI_ARGS_TAKEN = 0,    /* out holds the line, a space and the property's text */
	FUXI_ARGS_EMPTY,        /* the property is a lone NUL: nothing to add */
	FUXI_ARGS_NO_NUL,       /* no NUL inside the property */
	FUXI_ARGS_LONG,         /* more than FUXI_ARGS_MAX - 1 characters */
	FUXI_ARGS_BYTE,         /* a byte outside ' '..'~' before the NUL: a tab, a newline, UTF-8 */
	FUXI_ARGS_ROOM,         /* out is smaller than the line, a space, the text and a NUL */
};

static inline const char *
fuxi_args_why(int result)
{
	switch (result) {
	case FUXI_ARGS_TAKEN:
		return "taken";
	case FUXI_ARGS_EMPTY:
		return "it is empty";
	case FUXI_ARGS_NO_NUL:
		return "it has no NUL";
	case FUXI_ARGS_LONG:
		return "it is longer than 2047 characters";
	case FUXI_ARGS_BYTE:
		return "a byte in it is not printable ASCII";
	default:
		return "no room";
	}
}

/*
 * out = the line (up to its NUL, at most line_max - 1 characters), ' ', the property's text, NUL. On FUXI_ARGS_TAKEN
 * *at is where the text starts in out and *taken its length; on anything else both are 0 and out is untouched.
 */
static inline int
fuxi_args_merge(const char *line, unsigned int line_max, const void *prop, unsigned int size,
    char *out, unsigned int out_size, unsigned int *at, unsigned int *taken)
{
	const unsigned char *text = (const unsigned char *)prop;
	unsigned int length = 0, n, i;

	*at = 0;
	*taken = 0;
	for (n = 0; n < size && text[n] != '\0'; n++) {
		if (text[n] < 0x20 || text[n] > 0x7e) {
			return FUXI_ARGS_BYTE;
		}
	}
	if (n == size) {
		return FUXI_ARGS_NO_NUL;
	}
	if (n >= FUXI_ARGS_MAX) {
		return FUXI_ARGS_LONG;
	}
	if (n == 0) {
		return FUXI_ARGS_EMPTY;
	}
	while (length + 1 < line_max && line[length] != '\0') {
		length++;
	}
	if (out_size < 2 || length > out_size - 2 || n > out_size - 2 - length) {
		return FUXI_ARGS_ROOM;
	}
	for (i = 0; i < length; i++) {
		out[i] = line[i];
	}
	out[length] = ' ';
	for (i = 0; i < n; i++) {
		out[length + 1 + i] = (char)text[i];
	}
	out[length + 1 + n] = '\0';
	*at = length + 1;
	*taken = n;
	return FUXI_ARGS_TAKEN;
}

#endif /* FUXI_BOOT_ARGS_H */
