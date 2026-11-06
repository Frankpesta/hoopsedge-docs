// Fails if the API key or URL reaches anything the browser downloads (docs/10 section 9.1).
// Run after a build made with these exact sentinel values, so a match can only be a real leak.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

export const SENTINEL_KEY = "LEAKCHECK-KEY-7f3a9c";
export const SENTINEL_URL = "leakcheck-api.invalid";

function files(folder) {
  return readdirSync(folder).flatMap((name) => {
    const path = join(folder, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const root = ".next/static";
const scanned = files(root);
if (!scanned.length) {
  console.error(`Nothing to scan in ${root}: build first.`);
  process.exit(1);
}
const forbidden = [SENTINEL_KEY, SENTINEL_URL, "HOOPSEDGE_API_KEY", "HOOPSEDGE_API_URL"];
const leaks = scanned.flatMap((path) => {
  const text = readFileSync(path, "utf-8");
  return forbidden.filter((word) => text.includes(word)).map((word) => `${path}: ${word}`);
});
if (leaks.length) {
  console.error("Secret leak in client files:\n" + leaks.join("\n"));
  process.exit(1);
}
console.log(`No secrets in ${scanned.length} client files.`);
