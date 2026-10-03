export const legalContactEmail = "team@caid-technologies.com";
export const legalEntityName = "CAID TECHNOLOGIES, INC.";
export const legalLastUpdated = "July 31, 2026";
export const legalEffectiveDate = "July 8, 2026";

export type LegalSection = {
  heading: string;
  paragraphs?: string[];
  bullets?: string[];
};

export type LegalDocument = {
  slug: string;
  title: string;
  summary: string;
  sections: LegalSection[];
};

export const legalDocuments: LegalDocument[] = [
  {
    slug: "terms-of-service",
    title: "Terms of Service",
    summary: "The terms that govern access to and use of Forma.",
    sections: [
      {
        heading: "The Service",
        paragraphs: [
          "Forma is an AI-assisted tool for generating low-voltage maker electronics project information, including structured hardware descriptions, parts lists, wiring notes, validation warnings, assembly notes, diagrams, and concept images.",
          "Generated content can be incomplete, inaccurate, unsafe, non-unique, or unsuitable for a particular purpose. You are responsible for independent review, testing, and professional validation before building, purchasing parts for, manufacturing, selling, or deploying any generated design.",
        ],
      },
      {
        heading: "Eligibility",
        paragraphs: [
          "You must be at least 13 years old to use the Service. If you are under the age of majority where you live, you may use the Service only with permission and supervision from a parent or legal guardian. The Service is not directed to children under 13.",
        ],
      },
      {
        heading: "Accounts, Content, and Outputs",
        paragraphs: [
          "You are responsible for activity under your account and for keeping credentials, API keys, integration tokens, and devices secure.",
          "You grant Forma permission to host, store, reproduce, process, transmit, display, and use prompts, uploads, project data, and related content as needed to provide, secure, improve, and support the Service.",
          "As between you and Forma, you may use generated outputs for lawful purposes, subject to these terms and applicable third-party rights or provider terms.",
        ],
      },
      {
        heading: "Hardware Safety",
        paragraphs: [
          "Forma is not a substitute for professional engineering review, certified electrical design, laboratory testing, safety certification, legal advice, medical advice, or product compliance work.",
        ],
        bullets: [
          "Do not use Forma for mains AC, high-voltage, high-current, or unsafe battery systems without qualified professional oversight.",
          "Do not use Forma for weapons, harmful devices, medical devices, automotive systems, aviation systems, industrial controls, public infrastructure, or other safety-critical systems.",
          "Do not sell or deploy products based on Forma outputs without qualified engineering, regulatory, and safety review.",
        ],
      },
      {
        heading: "Third-Party Services",
        paragraphs: [
          "The Service may rely on model providers, image providers, hosting providers, databases, analytics, authentication, email, storage, payment processors, suppliers, and marketplaces. Third-party services are governed by their own terms, privacy policies, fees, data practices, and availability.",
        ],
      },
      {
        heading: "Disclaimers and Liability",
        paragraphs: [
          "The Service and outputs are provided as is and as available. To the maximum extent permitted by law, Forma disclaims warranties of merchantability, fitness for a particular purpose, title, non-infringement, accuracy, safety, availability, and quiet enjoyment.",
          "To the maximum extent permitted by law, Forma will not be liable for indirect, incidental, special, consequential, exemplary, or punitive damages, or for lost profits, lost data, business interruption, personal injury, property damage, product liability, or cost of substitute services.",
        ],
      },
      {
        heading: "Contact",
        paragraphs: [`Questions about these terms can be sent to ${legalContactEmail}.`],
      },
    ],
  },
  {
    slug: "privacy-policy",
    title: "Privacy Policy",
    summary: "How Forma collects, uses, shares, and protects information.",
    sections: [
      {
        heading: "Information We Collect",
        bullets: [
          "Account and contact information, such as name, email address, organization, role, and support details.",
          "Authentication and integration information, including provider selections, masked credential status, and API keys or tokens you choose to save.",
          "Project and generation content, including prompts, uploaded images, chat history, generated hardware intermediate representation, BOMs, diagrams, validation results, assembly notes, and concept images.",
          "Usage, device, log, diagnostic, and communication information.",
        ],
      },
      {
        heading: "How We Use Information",
        paragraphs: [
          "We use information to provide, operate, maintain, secure, debug, improve, and support the Service; create and store generated projects; route requests to configured providers; communicate with users; enforce policies; comply with law; and develop aggregated or de-identified analytics.",
          "A deleted project is not used as a research contribution unless you separately and explicitly opt in for that project. General acceptance of the Terms of Service is not contribution consent.",
        ],
      },
      {
        heading: "AI Providers",
        paragraphs: [
          "Forma may send prompts, uploaded images, project content, and related metadata to AI model providers, image generation providers, hosting providers, or compatible infrastructure selected or configured for a generation request.",
          "Provider data practices may vary. Review provider terms and privacy policies before using live model, image, storage, or infrastructure providers.",
        ],
      },
      {
        heading: "Disclosure",
        paragraphs: [
          "We may disclose information to service providers, third-party services you connect or select, other users or the public if you intentionally share content, legal authorities when required, parties involved in business transfers, and others with your consent.",
          "We do not sell personal information for money. If our practices change in a way that requires additional choices, we will update this policy.",
        ],
      },
      {
        heading: "Retention and Security",
        paragraphs: [
          "Deleting a project removes it from normal product access immediately. Its identifiable operational copy is normally scheduled for permanent deletion after a 30-day retention period, unless the published retention configuration changes or a narrowly applicable legal or security obligation requires otherwise. During that window, restoration may be available until purge begins.",
          "Deletion covers active project records, project-linked conversations, jobs, generated images and videos, and other active project storage. Encrypted backups expire under the backup provider's retention schedule and are not restored into normal product access; a disaster recovery restore must reapply deletion records before service resumes.",
          "If you separately opt in, we may create an aggregate sanitized contribution that excludes account and workspace identifiers, prompts, titles, names, URLs, credentials, uploads, and free text. Before irreversible anonymization you may withdraw permission and the pending contribution will be deleted. Once irreversibly anonymized, it cannot be linked back to you or selectively withdrawn.",
          "We use reasonable safeguards designed to protect information. No method of transmission or storage is completely secure.",
        ],
      },
      {
        heading: "Your Choices",
        bullets: [
          "You may request access, correction, deletion, export, restriction, portability, or objection where available under applicable law.",
          "You may control cookies and local storage through your browser.",
          "You may disconnect third-party integrations and opt out of non-essential emails.",
          "Project contribution is optional, is off by default, and does not affect deletion or core product functionality. Consent is purpose-specific and can be withdrawn before irreversible anonymization.",
        ],
      },
      {
        heading: "Children",
        paragraphs: [
          "The Service is not directed to children under 13, and we do not knowingly collect personal information from children under 13.",
        ],
      },
      {
        heading: "Contact",
        paragraphs: [`Privacy requests can be sent to ${legalContactEmail}.`],
      },
    ],
  },
  {
    slug: "data-contribution-terms",
    title: "Project Data Contribution Terms",
    summary: "Optional terms for contributing a sanitized project copy for approved research and evaluation purposes.",
    sections: [
      {
        heading: "Separate and Optional Choice",
        paragraphs: [
          "Project contribution is optional, off by default, and separate from deleting a project or accepting the general Terms of Service. Declining does not limit core Forma functionality.",
          "Your recorded consent identifies the policy version and the permitted purposes. Materially expanded uses require renewed consent.",
        ],
      },
      {
        heading: "What Is Retained",
        paragraphs: [
          "Forma retains only an aggregate hardware summary produced by the documented sanitization process. It excludes prompts, project titles, account and workspace identifiers, names, email and IP addresses, credentials, connection strings, repository information, URLs, uploads, file metadata, request metadata, and other free text.",
          "The sanitized contribution is not made public, sold, or licensed to third parties under these terms.",
        ],
      },
      {
        heading: "Permitted Purposes",
        bullets: [
          "Product research and reliability testing.",
          "Evaluation datasets and model evaluation.",
          "AI system and product improvement when that purpose is included in your recorded consent.",
        ],
      },
      {
        heading: "Ownership and Limited License",
        paragraphs: [
          "You retain ownership of content you own. You grant CAID Technologies a non-exclusive, worldwide, royalty-free license to process the sanitized contribution solely for the purposes listed in your recorded consent.",
          "You represent that you have the right to contribute the content. Do not contribute employer-confidential information or third-party personal information without authorization.",
        ],
      },
      {
        heading: "Withdrawal and Anonymization",
        paragraphs: [
          "You may withdraw consent before irreversible anonymization. Withdrawal deletes any identifiable or pseudonymized pending contribution and prevents its future dataset use.",
          "After irreversible anonymization, the contribution is severed from project, workspace, consent, and account identifiers, so it can no longer be located as yours or selectively withdrawn. Existing aggregate evaluations or completed model artifacts cannot necessarily be reversed, but a withdrawn pending contribution will not be used in future datasets.",
        ],
      },
      {
        heading: "Contact",
        paragraphs: [`Questions or withdrawal requests can be sent to ${legalContactEmail}.`],
      },
    ],
  },
  {
    slug: "acceptable-use-policy",
    title: "Acceptable Use Policy",
    summary: "Rules for safe, lawful, and respectful use of Forma.",
    sections: [
      {
        heading: "Safety-First Scope",
        paragraphs: [
          "Forma is intended for low-voltage maker electronics, educational prototypes, and early design exploration. You may not use the Service to create or facilitate unsafe, illegal, abusive, or rights-violating activity.",
        ],
      },
      {
        heading: "Prohibited Hardware Uses",
        bullets: [
          "Weapons, explosives, traps, harmful devices, or systems intended to injure people or damage property.",
          "Mains AC, high-voltage, high-current, unsafe battery, or high-power energy systems without qualified professional oversight.",
          "Medical, automotive, aviation, railway, maritime, industrial control, public infrastructure, emergency response, or other safety-critical systems.",
          "Unlawful surveillance, stalking, credential theft, evasion, sabotage, physical intrusion, or safety bypass systems.",
        ],
      },
      {
        heading: "Prohibited Conduct",
        bullets: [
          "Violating laws, sanctions, export controls, court orders, or third-party rights.",
          "Submitting infringing, private, confidential, harmful, exploitative, hateful, harassing, or deceptive content.",
          "Bypassing rate limits, access controls, safety controls, payment controls, model restrictions, or usage limits.",
          "Using stolen or unauthorized API keys or abusing model, image, cloud, marketplace, or integration providers.",
        ],
      },
      {
        heading: "Responsible Use",
        paragraphs: [
          "You are responsible for independently verifying component ratings, datasheets, pinouts, wiring, polarity, grounding, fusing, battery protection, supplier reliability, legal requirements, and product safety requirements before using outputs.",
        ],
      },
      {
        heading: "Reporting",
        paragraphs: [`Report abuse, safety concerns, or security issues to ${legalContactEmail}.`],
      },
    ],
  },
  {
    slug: "hardware-safety-disclaimer",
    title: "Hardware Safety Disclaimer",
    summary: "Important limits for AI-generated hardware plans.",
    sections: [
      {
        heading: "Not Professional Engineering Advice",
        paragraphs: [
          "Forma outputs are not professional electrical, mechanical, industrial, product safety, legal, regulatory, medical, or manufacturing advice. Generated designs, diagrams, BOMs, validation reports, and assembly notes may be wrong, incomplete, unsafe, outdated, or unsuitable for your intended use.",
        ],
      },
      {
        heading: "Intended Scope",
        paragraphs: [
          "Forma is intended for low-voltage maker electronics, typically 3.3V to 5V DC educational prototypes and hobby projects.",
        ],
      },
      {
        heading: "Do Not Use For",
        bullets: [
          "Mains AC wiring or line-powered products.",
          "High-voltage, high-current, high-temperature, or high-energy systems.",
          "Lithium battery packs, charging, or power systems without qualified review.",
          "Medical, automotive, aviation, industrial, public infrastructure, life-safety, weapons, or harmful-device systems.",
          "Commercial products without formal engineering, testing, and certification.",
        ],
      },
      {
        heading: "Required Independent Checks",
        paragraphs: [
          "Before purchasing parts or building anything, verify datasheets, manufacturer application notes, voltage, current, power, thermal limits, pinouts, polarity, grounding, battery chemistry, enclosure safety, mechanical tolerances, and regulatory obligations.",
        ],
      },
      {
        heading: "Contact",
        paragraphs: [`Report dangerous outputs or safety issues to ${legalContactEmail}.`],
      },
    ],
  },
  {
    slug: "cookie-and-local-storage-notice",
    title: "Cookie and Local Storage Notice",
    summary: "How Forma may use browser storage and similar technologies.",
    sections: [
      {
        heading: "How Forma Uses Storage",
        bullets: [
          "Keeping users signed in and supporting secure sessions.",
          "Remembering settings, preferences, selected project state, and UI state.",
          "Storing local chat or project drafts in the browser.",
          "Measuring usage, diagnosing errors, improving reliability, and preventing abuse.",
        ],
      },
      {
        heading: "Categories",
        bullets: [
          "Strictly necessary storage for security and core Service functionality.",
          "Functional storage for preferences, project state, local chat history, and UI settings.",
          "Analytics or marketing storage only if those tools are enabled and required consent is obtained.",
        ],
      },
      {
        heading: "Your Choices",
        paragraphs: [
          "You can block or delete cookies and local storage in your browser settings. Some Service features may not work correctly if necessary storage is disabled or cleared.",
        ],
      },
      {
        heading: "Contact",
        paragraphs: [`Questions about cookies or local storage can be sent to ${legalContactEmail}.`],
      },
    ],
  },
  {
    slug: "copyright-dmca-policy",
    title: "Copyright and DMCA Policy",
    summary: "How to report copyright concerns.",
    sections: [
      {
        heading: "Copyright Complaints",
        paragraphs: [
          "If you believe content available through the Service infringes your copyright, send a notice with your signature, identification of the copyrighted work, identification of the allegedly infringing material, your contact details, your good-faith statement, and your accuracy statement under penalty of perjury.",
        ],
      },
      {
        heading: "Counter-Notices",
        paragraphs: [
          "If you believe content was removed by mistake or misidentification, send a counter-notice with your contact information, identification of the removed material, a jurisdiction statement, and your statement under penalty of perjury that the material was removed by mistake or misidentification.",
        ],
      },
      {
        heading: "Repeat Infringers",
        paragraphs: [
          "Forma may suspend or terminate accounts of repeat infringers and may remove content that appears to infringe intellectual property rights.",
        ],
      },
      {
        heading: "Contact",
        paragraphs: [`Copyright notices can be sent to ${legalContactEmail}.`],
      },
    ],
  },
  {
    slug: "security-policy",
    title: "Security and Vulnerability Disclosure Policy",
    summary: "How to report vulnerabilities safely.",
    sections: [
      {
        heading: "Reporting",
        paragraphs: [`Send vulnerability reports to ${legalContactEmail}. Include affected URLs, API routes, packages, reproduction steps, impact, evidence, and whether any data that was not yours was accessed.`],
      },
      {
        heading: "In Scope",
        bullets: [
          "The public Forma web application.",
          "Documented Forma APIs.",
          "Authentication, authorization, project access, and integration handling.",
          "Public repository code maintained by Forma.",
        ],
      },
      {
        heading: "Out of Scope",
        bullets: [
          "Denial-of-service or load testing.",
          "Social engineering, phishing, or physical attacks.",
          "Attacks against third-party providers or suppliers.",
          "Accessing, modifying, deleting, or exfiltrating data that is not yours.",
          "Automated scanning that degrades the Service.",
        ],
      },
      {
        heading: "Safe Harbor",
        paragraphs: [
          "If you act in good faith, follow this policy, avoid privacy violations and disruption, and report issues promptly, Forma will not pursue legal action against you for the research itself.",
        ],
      },
    ],
  },
  {
    slug: "accessibility-statement",
    title: "Accessibility Statement",
    summary: "Forma's accessibility commitment and feedback path.",
    sections: [
      {
        heading: "Commitment",
        paragraphs: [
          "Forma is committed to making the Service accessible and usable for as many people as possible.",
          "Our goal is to substantially conform to the Web Content Accessibility Guidelines (WCAG) 2.2 Level AA where practical for the current product stage.",
        ],
      },
      {
        heading: "Ongoing Work",
        paragraphs: [
          "We intend to improve accessibility through design reviews, semantic markup, keyboard navigation, color contrast checks, responsive layouts, alternative text, clear focus states, and testing with assistive technologies.",
        ],
      },
      {
        heading: "Known Limitations",
        paragraphs: [
          "Some generated diagrams, images, 3D scenes, canvas views, or third-party embedded content may not yet provide equivalent accessible alternatives. We will prioritize reasonable fixes when issues are reported.",
        ],
      },
      {
        heading: "Feedback",
        paragraphs: [`If you experience an accessibility barrier, contact ${legalContactEmail} with the page or feature, assistive technology or browser details if relevant, and a short description of the problem.`],
      },
    ],
  },
];

export const primaryLegalSlugs = [
  "terms-of-service",
  "privacy-policy",
  "data-contribution-terms",
  "acceptable-use-policy",
  "hardware-safety-disclaimer",
  "cookie-and-local-storage-notice",
] as const;

export const secondaryLegalSlugs = [
  "copyright-dmca-policy",
  "security-policy",
  "accessibility-statement",
] as const;

export function getLegalDocument(slug: string) {
  return legalDocuments.find((document) => document.slug === slug) || null;
}

export function documentsForSlugs(slugs: readonly string[]) {
  return slugs
    .map((slug) => legalDocuments.find((document) => document.slug === slug))
    .filter((document): document is LegalDocument => Boolean(document));
}

export function legalDocumentSearchText(document: LegalDocument) {
  return [
    document.title,
    document.summary,
    ...document.sections.flatMap((section) => [
      section.heading,
      ...(section.paragraphs || []),
      ...(section.bullets || []),
    ]),
  ].join("\n");
}

export function legalDocumentMatchesQuery(document: LegalDocument, query: string) {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return legalDocumentSearchText(document).toLowerCase().includes(needle);
}
