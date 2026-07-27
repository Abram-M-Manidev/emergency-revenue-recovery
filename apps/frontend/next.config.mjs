/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  // Baseline security headers (Milestone 10). No Content-Security-Policy
  // here — a real CSP needs per-route nonce wiring through middleware.ts
  // plus an audit of every inline style/script path; out of scope for
  // this pass. HSTS is also omitted: that's a deployment/TLS-layer
  // concern, not something this app's own config should assert.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "geolocation=(), microphone=(), camera=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
