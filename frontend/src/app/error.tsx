"use client";

import { useEffect } from "react";
import Link from "next/link";
import { AlertCircle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface ErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function Error({ error, reset }: ErrorProps) {
  useEffect(() => {
    // 배포 직후 chunk hash 불일치 / SSR mismatch / 일시적 fetch 실패 등
    // 모든 에러에 대해 자동 1회 reload 시도 (60초 가드로 무한 loop 차단).
    // chunk error 한정 검사 폐기: 실측에서 사용자가 보는 "오류 화면" 대부분이
    // chunk 패턴 미매칭 (SSR boundary 또는 dynamic import 외) → reload 무조건 시도.
    if (typeof window !== "undefined") {
      try {
        const key = "samba.errorReloadAt";
        const last = Number(window.sessionStorage.getItem(key) || "0");
        if (Date.now() - last > 60_000) {
          window.sessionStorage.setItem(key, String(Date.now()));
          window.location.reload();
          return;
        }
      } catch {
        /* ignore */
      }
    }
    console.error("Application error:", error);
  }, [error]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <Card className="w-full max-w-md shadow-lg">
        <CardHeader className="text-center">
          <div className="mx-auto w-12 h-12 bg-destructive/10 rounded-full flex items-center justify-center mb-4">
            <AlertCircle className="w-6 h-6 text-destructive" />
          </div>
          <CardTitle className="text-xl">오류가 발생했습니다</CardTitle>
          <CardDescription>
            페이지를 불러오는 중 문제가 발생했습니다.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {process.env.NODE_ENV === "development" && (
            <div className="p-3 bg-muted rounded-lg">
              <p className="text-xs text-muted-foreground font-mono break-all">
                {error.message}
              </p>
            </div>
          )}
          <div className="flex flex-col gap-2">
            <Button onClick={reset} className="w-full gap-2">
              <RefreshCw className="w-4 h-4" />
              다시 시도
            </Button>
            <Button variant="outline" asChild className="w-full">
              <Link href="/">홈으로 돌아가기</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
