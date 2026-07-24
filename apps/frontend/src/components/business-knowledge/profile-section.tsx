"use client";

import { useEffect, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchBusinessProfile, updateBusinessProfile } from "@/lib/api/business-knowledge";
import { ApiError } from "@/lib/api/client";
import type { BusinessType } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const BUSINESS_TYPES: { value: BusinessType; label: string }[] = [
  { value: "hvac", label: "HVAC" },
  { value: "plumbing", label: "Plumbing" },
  { value: "electrical", label: "Electrical" },
  { value: "other", label: "Other" },
];

const nullableString = (max: number) =>
  z
    .string()
    .max(max)
    .nullish()
    .transform((value) => (value && value.trim() !== "" ? value : null));

const profileSchema = z.object({
  business_type: z.enum(["hvac", "plumbing", "electrical", "other"]),
  display_name: z.string().min(1, "Display name is required.").max(255),
  phone_number: nullableString(32),
  timezone: z.string().min(1, "Timezone is required.").max(64),
  address_line1: nullableString(255),
  address_line2: nullableString(255),
  city: nullableString(120),
  state: nullableString(60),
  postal_code: nullableString(20),
  country: z.string().min(2).max(2).default("US"),
  website: nullableString(255),
});

type ProfileValues = z.infer<typeof profileSchema>;

const EMPTY_VALUES: ProfileValues = {
  business_type: "hvac",
  display_name: "",
  phone_number: null,
  timezone: "",
  address_line1: null,
  address_line2: null,
  city: null,
  state: null,
  postal_code: null,
  country: "US",
  website: null,
};

export function ProfileSection() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);

  const form = useForm<ProfileValues>({
    resolver: zodResolver(profileSchema),
    defaultValues: EMPTY_VALUES,
  });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const profile = await fetchBusinessProfile();
        if (cancelled) return;
        form.reset({
          business_type: profile.business_type,
          display_name: profile.display_name,
          phone_number: profile.phone_number,
          timezone: profile.timezone,
          address_line1: profile.address_line1,
          address_line2: profile.address_line2,
          city: profile.city,
          state: profile.state,
          postal_code: profile.postal_code,
          country: profile.country,
          website: profile.website,
        });
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) {
          toast({ title: "Failed to load business profile", variant: "destructive" });
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onSubmit(values: ProfileValues) {
    try {
      await updateBusinessProfile(values);
      toast({ title: "Business profile saved", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to save business profile",
        variant: "destructive",
      });
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Business profile</CardTitle>
        <CardDescription>
          The identity and contact details the AI will use when answering calls.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : (
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="grid gap-4 sm:grid-cols-2" noValidate>
              <FormField
                control={form.control}
                name="business_type"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Business type</FormLabel>
                    <FormControl>
                      <select
                        {...field}
                        className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                      >
                        {BUSINESS_TYPES.map((type) => (
                          <option key={type.value} value={type.value}>
                            {type.label}
                          </option>
                        ))}
                      </select>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="display_name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Display name</FormLabel>
                    <FormControl>
                      <Input placeholder="Acme HVAC Services" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="phone_number"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Phone number</FormLabel>
                    <FormControl>
                      <Input placeholder="+1 555 123 4567" {...field} value={field.value ?? ""} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="timezone"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Timezone (IANA)</FormLabel>
                    <FormControl>
                      <Input placeholder="America/Chicago" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="address_line1"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Address line 1</FormLabel>
                    <FormControl>
                      <Input {...field} value={field.value ?? ""} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="address_line2"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Address line 2</FormLabel>
                    <FormControl>
                      <Input {...field} value={field.value ?? ""} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="city"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>City</FormLabel>
                    <FormControl>
                      <Input {...field} value={field.value ?? ""} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="state"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>State</FormLabel>
                    <FormControl>
                      <Input {...field} value={field.value ?? ""} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="postal_code"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Postal code</FormLabel>
                    <FormControl>
                      <Input {...field} value={field.value ?? ""} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="website"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Website</FormLabel>
                    <FormControl>
                      <Input placeholder="https://" {...field} value={field.value ?? ""} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <div className="sm:col-span-2">
                <Button type="submit" isLoading={form.formState.isSubmitting}>
                  Save profile
                </Button>
              </div>
            </form>
          </Form>
        )}
      </CardContent>
    </Card>
  );
}
