"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  type HTMLAttributes,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";

import { cn } from "@/lib/utils/cn";

interface DialogContextValue {
  onOpenChange: (open: boolean) => void;
}

const DialogContext = createContext<DialogContextValue | undefined>(undefined);

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}

/** Low-level modal primitive: overlay + portal + escape-to-close.
 * For the common "title/description/footer" shape, prefer `Modal`. */
export function Dialog({ open, onOpenChange, children }: DialogProps) {
  const close = useCallback(() => onOpenChange(false), [onOpenChange]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = "";
    };
  }, [open, close]);

  if (typeof document === "undefined") return null;

  return createPortal(
    <DialogContext.Provider value={{ onOpenChange }}>
      <AnimatePresence>
        {open ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="absolute inset-0 bg-black/50"
              onClick={close}
              aria-hidden="true"
            />
            <motion.div
              role="dialog"
              aria-modal="true"
              initial={{ opacity: 0, scale: 0.98, y: 8 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.98, y: 8 }}
              transition={{ duration: 0.15 }}
              className="relative z-10 w-full max-w-md"
            >
              {children}
            </motion.div>
          </div>
        ) : null}
      </AnimatePresence>
    </DialogContext.Provider>,
    document.body,
  );
}

export function DialogContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  const context = useContext(DialogContext);
  return (
    <div
      className={cn(
        "relative rounded-lg border border-border bg-card p-6 text-card-foreground shadow-lg",
        className,
      )}
      {...props}
    >
      {props.children}
      {context ? (
        <button
          type="button"
          onClick={() => context.onOpenChange(false)}
          className="absolute right-4 top-4 text-muted-foreground transition hover:text-foreground"
          aria-label="Close dialog"
        >
          <X className="h-4 w-4" />
        </button>
      ) : null}
    </div>
  );
}

export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mb-4 flex flex-col gap-1.5", className)} {...props} />;
}

export function DialogTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-lg font-semibold leading-none", className)} {...props} />;
}

export function DialogDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mt-6 flex justify-end gap-2", className)} {...props} />;
}
