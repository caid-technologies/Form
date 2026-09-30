"use client";

import type { ChangeEventHandler, RefObject } from "react";
import Image from "next/image";
import { FileText, Paperclip, X } from "lucide-react";

export type ChatAttachmentControls = {
  imageInputRef: RefObject<HTMLInputElement | null>;
  onImageChange: ChangeEventHandler<HTMLInputElement>;
  selectedImage: string | null;
  selectedDocumentName: string | null;
  onRemoveImage: () => void;
  onRemoveDocument: () => void;
};

// Selection lives in the workspace so changing composer layouts cannot lose it.
export function ChatAttachmentSelection({
  imageInputRef, onImageChange, selectedImage, selectedDocumentName, onRemoveImage, onRemoveDocument,
}: ChatAttachmentControls) {
  return <>
    <input ref={imageInputRef} type="file" aria-label="Choose image or PDF" accept="image/*,application/pdf,.pdf" onChange={onImageChange} className="hidden" />
    {selectedImage && (
      <div className="mb-2 flex items-center gap-2">
        <div className="relative h-20 w-20 shrink-0">
          <Image
            src={selectedImage}
            alt="Attached prompt image"
            width={80}
            height={80}
            unoptimized
            className="h-20 w-20 rounded-xl border border-white/10 bg-black/20 object-cover"
          />
          <button
            type="button"
            onClick={onRemoveImage}
            className="absolute -right-1.5 -top-1.5 flex h-6 w-6 items-center justify-center rounded-full border border-white/15 bg-[#2f3238] text-zinc-200 shadow-lg transition-colors hover:bg-[#3a3d44] hover:text-white"
            aria-label="Remove image"
            title="Remove image"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    )}
    {selectedDocumentName && (
      <div className="mb-2 flex items-start gap-2 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-2 pr-2">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[var(--forma-page)] text-zinc-300">
          <FileText className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1 py-0.5">
          <div className="truncate text-xs font-medium text-[var(--forma-text-strong)]">{selectedDocumentName}</div>
          <div className="mt-0.5 text-[11px] leading-4 text-[var(--forma-text-muted)]">
            PDF context · text and relevant visual pages will be extracted before the build.
          </div>
        </div>
        <button
          type="button"
          onClick={onRemoveDocument}
          className="rounded-md p-1.5 text-[var(--forma-text-muted)] transition-colors hover:bg-[var(--forma-page)] hover:text-[var(--forma-text-strong)]"
          aria-label="Remove PDF"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    )}
  </>;
}

export function ChatAttachmentButton({ imageInputRef }: Pick<ChatAttachmentControls, "imageInputRef">) {
  return (
    <button
      type="button"
      onClick={() => imageInputRef.current?.click()}
      className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-zinc-800/50 hover:text-zinc-200"
      aria-label="Attach image or PDF"
      title="Attach an image or PDF, or paste an image from your clipboard"
    >
      <Paperclip className="h-4 w-4" />
    </button>
  );
}
