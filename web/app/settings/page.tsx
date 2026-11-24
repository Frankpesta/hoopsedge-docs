import { ConfigForm } from "@/components/ConfigForm";
import { Card, PageTitle, Section } from "@/components/ui";
import { apiGetFresh } from "@/lib/api";
import { lagosDateTime } from "@/lib/format";
import type { ConfigDocument, ConfigVersion, ConfigView } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function ConfigPage() {
  const [config, versions] = await Promise.all([
    apiGetFresh<ConfigView>("/config"),
    apiGetFresh<ConfigVersion[]>("/config/versions?limit=20"),
  ]);
  return (
    <>
      <PageTitle
        title="Config"
        sub={
          config.version
            ? `Version ${config.version}, saved ${lagosDateTime(config.changed_at)}. Runs use the version in force when they start.`
            : "The brief's starting values (version 0). Every save is a new version."
        }
      />
      {/* No key on the version: remounting after a save would wipe its confirmation. */}
      <ConfigForm
        version={config.version}
        document={config.document as unknown as ConfigDocument}
      />
      <Section title="History">
        {versions.length ? (
          <ul className="space-y-2">
            {versions.map((item) => (
              <li key={item.version}>
                <Card className="text-sm">
                  <p className="font-semibold">
                    Version {item.version}{" "}
                    <span className="text-text-2 font-normal">
                      · {lagosDateTime(item.changed_at)} · {item.changed_by ?? "unknown"}
                    </span>
                  </p>
                  <ul className="num text-text-2 mt-1 space-y-0.5 text-xs">
                    {(item.diff as { path: string; from: unknown; to: unknown }[]).map((change) => (
                      <li key={change.path} className="break-all">
                        {change.path}: {String(change.from)} {"→"} {String(change.to)}
                      </li>
                    ))}
                  </ul>
                </Card>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-text-2 text-sm">No changes saved yet.</p>
        )}
      </Section>
    </>
  );
}
