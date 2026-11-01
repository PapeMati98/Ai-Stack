# Optional upstream source classification

`typescript-5.9.3.json` classifies the credential-assignment precheck hits in one exact vendored TypeScript compiler. Its 219 matched lines describe compiler lexical tokens and AST properties. The code matches the official TypeScript 5.9.3 npm archive after CRLF-to-LF normalization; the existing upstream license and third-party notices are retained.

The policy is optional. Pass it explicitly to the release workbench privacy-policy input; it is not loaded by agent sessions or enabled globally.

`reviewed-source.json` includes that compiler entry and two exact source-code classifications: an empty token list in the Chrome launcher and transport assignments from `args.token` in the Office client. Those five additional matches contain code references rather than embedded credential values. Each entry has its own complete-file SHA-256 and the same single-rule restriction. The remaining source findings still require review.

The entry matches one rule, literal relative path and SHA-256 of the complete file. A byte change, another path or another rule does not match it. Private-key, provider-token and all unrelated checks remain active. The policy neither changes the compiler nor executes or installs downloaded code.

This is a source-code classification, not a complete secret scan, project license decision or permission to publish. Reassess the entry when changing the vendored compiler; do not expand it to a directory or filename pattern.
