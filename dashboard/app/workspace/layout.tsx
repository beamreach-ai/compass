import WorkspaceShell from '../components/WorkspaceShell';

export default function AppLayout({ children }: { children: React.ReactNode }) {
  // the demo marks itself in the shell (badge, title) and in the page (banner, watermark); docs/demo.md
  const demo = (process.env.APP_MODE ?? 'production') === 'demo';

  return (
    <div className="min-h-screen">
      <WorkspaceShell demo={demo}>
        <main>{children}</main>
      </WorkspaceShell>
    </div>
  );
}
