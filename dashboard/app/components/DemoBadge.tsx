// The DEMO chip: beside the account in the sidebar and in the phone's top bar, so no
// screenshot or recording passes for someone's own account. Not dismissable.
export default function DemoBadge({ className = "" }: { className?: string }) {
  return (
    <span
      data-demo-badge
      title="Demo account"
      className={`inline-flex items-center rounded-full border border-amber-300 bg-amber-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-amber-800 ${className}`}
    >
      Demo
    </span>
  );
}
