# TypeScript Parser

- Upstream: Microsoft/TypeScript, version 5.9.3, Apache-2.0.
- Initial vendoring reused an existing local TypeScript installation; no package
  install, lifecycle hook or source-project execution was performed.
- Provenance was subsequently checked against the official npm 5.9.3 archive
  using its registry SHA-512 integrity. The archive was inspected in RAM only.
- Historical vendored typescript.js raw SHA-256 (CRLF): f722647f0903c50a673288f2fb17ebb81ad8be458a8e32c139da7278ad9c044f.
- Current vendored bytes are the official LF file above; exactly 200276 CRLF sequences were replaced with LF.
- Official npm lib/typescript.js raw SHA-256 (LF): 3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675.
- Replacing only CRLF with LF in the complete vendored file yields exactly the
  official upstream SHA-256. Raw-byte identity is not claimed across those EOLs.
- LICENSE.txt and ThirdPartyNoticeText.txt are retained verbatim.
- Only createSourceFile is used. No tsconfig, plugin, import or compiler program
  is loaded. The parser is bundled rather than depending on a mutable project path.
- Node is required for an exception. Parser failure preserves the generic denial.
- This is an explicit local dependency addition to the hotfix, superseding the
  earlier design preference for no new dependencies. No provider request is made.
