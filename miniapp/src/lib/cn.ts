import { clsx, type ClassValue } from "clsx";

// Склейка классов (тонкая обёртка над clsx — единая точка на будущее).
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
