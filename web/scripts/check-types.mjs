// Fails if lib/api-types.ts doesn't match docs/api/openapi.json (docs/09: schema drift fails CI).
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const fresh = join(mkdtempSync(join(tmpdir(), "types-")), "api-types.ts");
execFileSync("node_modules/.bin/openapi-typescript", ["../docs/api/openapi.json", "-o", fresh], {
  stdio: "pipe",
});
if (readFileSync(fresh, "utf-8") !== readFileSync("lib/api-types.ts", "utf-8")) {
  console.error("lib/api-types.ts is out of date. Run: npm run types:generate");
  process.exit(1);
}
console.log("API types match the schema.");
