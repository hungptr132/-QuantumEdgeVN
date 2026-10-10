import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Generated from contracts/factsheet.v1.schema.json by `npm run gen:types`.
    // json-schema-to-typescript writes its own `/* eslint-disable */` header, which
    // eslint then reports as an unused directive whenever the emitted file happens to
    // break no rules. Hand-fixing it would be overwritten on the next generate, and the
    // file is not ours to style - the schema is the reviewable artifact.
    "src/lib/generated/**",
  ]),
]);

export default eslintConfig;
