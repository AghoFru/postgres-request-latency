#!/bin/sh
set -eu

php artisan config:cache
exec php artisan serve --host=0.0.0.0 --port=8000
