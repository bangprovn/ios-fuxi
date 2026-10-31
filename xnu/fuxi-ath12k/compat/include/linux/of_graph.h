/*
 * Stand-in: of_graph describes WSI links between chips of a multi-device group. No device node
 * exists in this domain (dev->of_node is NULL), and core.c does not reach these when that holds.
 * Each answers "no such node", the true answer for a single chip without a device tree.
 */
#ifndef _FA_LINUX_OF_GRAPH_H
#define _FA_LINUX_OF_GRAPH_H
#include <linux/of.h>
static inline struct device_node *
of_graph_get_endpoint_by_regs(const struct device_node *parent, int port, int endpoint)
{
	return (NULL);
}
static inline struct device_node *
of_graph_get_remote_endpoint(const struct device_node *node)
{
	return (NULL);
}
static inline struct device_node *
of_graph_get_port_parent(struct device_node *node)
{
	return (NULL);
}
#endif
