import type { Metadata } from "next";
import SharedProject from "../../../../forma-workspace/shared-project";

export const metadata: Metadata = {
  title: "Shared project | Forma",
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

/** Shared projects intentionally render outside the private workspace shell. */
export default async function SharedProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return <SharedProject projectId={projectId} />;
}
