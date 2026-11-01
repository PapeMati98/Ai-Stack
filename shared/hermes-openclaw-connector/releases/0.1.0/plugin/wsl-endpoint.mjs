// Use the kernel's current WSL gateway; no arbitrary URL, DNS or remembered IP.
export function endpointFromRoutes(text) {
  if (typeof text !== 'string' || text.length > 16384) throw new Error('wsl_route_invalid');
  const routes = text.trim().split(/\r?\n/).slice(1).map(line => line.trim().split(/\s+/));
  const defaults = routes.filter(r => r[1] === '00000000' && (Number.parseInt(r[3], 16) & 3) === 3);
  if (defaults.length !== 1 || defaults[0][0] !== 'eth0' || !/^[0-9A-Fa-f]{8}$/.test(defaults[0][2])) {
    throw new Error('wsl_route_ambiguous_or_missing');
  }
  const octets = defaults[0][2].match(/../g).reverse().map(n => Number.parseInt(n, 16));
  const [a, b] = octets;
  if (!(a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168))) {
    throw new Error('wsl_gateway_not_private');
  }
  return `http://${octets.join('.')}:18879/ask`;
}
