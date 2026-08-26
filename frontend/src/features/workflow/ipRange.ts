// Auto-subnet helper for the IP Discovery setup form. The configured Source
// Interface is stored as a CIDR (e.g. "10.0.10.5/24"), which already encodes the
// subnet, so the scan range can be prefilled from it without a second lookup.

function intToIp(n: number): string {
  return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".");
}

/**
 * Usable host range for a NIC CIDR: "10.0.10.5/24" -> { start: "10.0.10.1",
 * end: "10.0.10.254" }. Returns null for anything that is not an IPv4 CIDR
 * (e.g. "Auto", "", or a bare address), so callers leave the field untouched.
 * /31 and /32 have no separate host range, so the address(es) themselves are used.
 */
export function hostRangeFromCidr(cidr: string): { start: string; end: string } | null {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(cidr.trim());
  if (!match) return null;
  const octets = [match[1], match[2], match[3], match[4]].map(Number);
  const prefix = Number(match[5]);
  if (octets.some((o) => o > 255) || prefix < 1 || prefix > 32) return null;

  const ipInt = octets.reduce((acc, o) => (acc * 256 + o) >>> 0, 0) >>> 0;
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  const network = (ipInt & mask) >>> 0;
  const broadcast = (network | (~mask >>> 0)) >>> 0;
  const size = 2 ** (32 - prefix);
  const first = size >= 4 ? (network + 1) >>> 0 : network;
  const last = size >= 4 ? (broadcast - 1) >>> 0 : broadcast;
  return { start: intToIp(first), end: intToIp(last) };
}
