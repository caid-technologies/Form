import Link from "next/link";
import {
  documentsForSlugs,
  legalContactEmail,
  legalEntityName,
  primaryLegalSlugs,
  secondaryLegalSlugs,
} from "../lib/legal-docs";

export default function LegalFooter() {
  const primaryFooterLinks = documentsForSlugs(primaryLegalSlugs);
  const secondaryFooterLinks = documentsForSlugs(secondaryLegalSlugs);

  return (
    <footer className="border-t border-[#2c2f37] bg-[#181b22] px-5 py-8 font-sans text-slate-400">
      <div className="mx-auto grid w-full max-w-7xl gap-6 md:grid-cols-[1fr_2fr]">
        <div>
          <Link href="/" className="text-sm font-semibold tracking-tight text-white">
            Forma
          </Link>
          <p className="mt-3 max-w-sm text-xs leading-5 text-slate-500">{legalEntityName}</p>
          <a
            href={`mailto:${legalContactEmail}`}
            className="mt-2 inline-block text-xs text-slate-400 hover:text-white"
          >
            {legalContactEmail}
          </a>
        </div>

        <nav className="grid gap-4 text-xs sm:grid-cols-2" aria-label="Legal">
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            {primaryFooterLinks.map((document) => (
              <Link key={document.slug} href={`/legal/${document.slug}`} className="hover:text-white">
                {document.title}
              </Link>
            ))}
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-2 sm:justify-end">
            <Link href="/install/opencode" className="hover:text-white">
              Install for OpenCode
            </Link>
            <Link href="/about" className="hover:text-white">
              About
            </Link>
            {secondaryFooterLinks.map((document) => (
              <Link key={document.slug} href={`/legal/${document.slug}`} className="hover:text-white">
                {document.title}
              </Link>
            ))}
          </div>
        </nav>
      </div>
    </footer>
  );
}
