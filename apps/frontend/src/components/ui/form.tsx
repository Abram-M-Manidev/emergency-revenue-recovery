"use client";

import { cloneElement, createContext, isValidElement, useContext, useId, type HTMLAttributes, type ReactElement } from "react";
import {
  Controller,
  FormProvider,
  useFormContext,
  type ControllerProps,
  type FieldPath,
  type FieldValues,
} from "react-hook-form";

import { cn } from "@/lib/utils/cn";

export const Form = FormProvider;

interface FormFieldContextValue {
  name: string;
}

const FormFieldContext = createContext<FormFieldContextValue | undefined>(undefined);

export function FormField<
  TFieldValues extends FieldValues = FieldValues,
  TName extends FieldPath<TFieldValues> = FieldPath<TFieldValues>,
>(props: ControllerProps<TFieldValues, TName>) {
  return (
    <FormFieldContext.Provider value={{ name: props.name }}>
      <Controller {...props} />
    </FormFieldContext.Provider>
  );
}

interface FormItemContextValue {
  id: string;
}

const FormItemContext = createContext<FormItemContextValue | undefined>(undefined);

function useFormField() {
  const fieldContext = useContext(FormFieldContext);
  const itemContext = useContext(FormItemContext);
  const { getFieldState, formState } = useFormContext();

  if (!fieldContext || !itemContext) {
    throw new Error("Form field hooks must be used within <FormField> and <FormItem>.");
  }

  const fieldState = getFieldState(fieldContext.name, formState);

  return {
    id: itemContext.id,
    name: fieldContext.name,
    formItemId: `${itemContext.id}-form-item`,
    formDescriptionId: `${itemContext.id}-form-item-description`,
    formMessageId: `${itemContext.id}-form-item-message`,
    ...fieldState,
  };
}

export function FormItem({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  const id = useId();
  return (
    <FormItemContext.Provider value={{ id }}>
      <div className={cn("flex flex-col gap-1.5", className)} {...props} />
    </FormItemContext.Provider>
  );
}

export function FormLabel({ className, ...props }: HTMLAttributes<HTMLLabelElement>) {
  const { formItemId, error } = useFormField();
  return (
    <label
      htmlFor={formItemId}
      className={cn("text-sm font-medium", error && "text-destructive", className)}
      {...props}
    />
  );
}

export function FormControl({ children }: { children: ReactElement }) {
  const { formItemId, formDescriptionId, formMessageId, error } = useFormField();
  if (!isValidElement(children)) return children;

  return cloneElement(children, {
    id: formItemId,
    "aria-describedby": error ? `${formDescriptionId} ${formMessageId}` : formDescriptionId,
    "aria-invalid": Boolean(error),
  } as Record<string, unknown>);
}

export function FormDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  const { formDescriptionId } = useFormField();
  return (
    <p id={formDescriptionId} className={cn("text-xs text-muted-foreground", className)} {...props} />
  );
}

export function FormMessage({ className, children, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  const { error, formMessageId } = useFormField();
  const body = error ? String(error.message ?? "") : children;
  if (!body) return null;

  return (
    <p id={formMessageId} className={cn("text-xs font-medium text-destructive", className)} {...props}>
      {body}
    </p>
  );
}
