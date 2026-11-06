import { SettingsForm } from "@/components/SettingsForm";
import { PageTitle } from "@/components/ui";
import { apiGetFresh } from "@/lib/api";
import { lagosDateTime } from "@/lib/format";
import type { SettingsView } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const settings = await apiGetFresh<SettingsView>("/config");
  return (
    <>
      <PageTitle
        title="Settings"
        sub={
          settings.version
            ? `Version ${settings.version}, changed ${lagosDateTime(settings.changed_at)}. Every change is recorded.`
            : "Phase 4's starting values. Every change is recorded."
        }
      />
      {/* No key on the version: remounting after a save would wipe its confirmation. */}
      <SettingsForm version={settings.version} document={settings.document} />
    </>
  );
}
