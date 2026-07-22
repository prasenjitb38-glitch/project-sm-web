import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

// The release URL can be overridden for another environment at build time.
const projectSmUrl = String.fromEnvironment(
  'PROJECT_SM_URL',
  defaultValue: 'https://project-sm-web.onrender.com',
);

void main() => runApp(const ProjectSmMobileApp());

class ProjectSmMobileApp extends StatelessWidget {
  const ProjectSmMobileApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Project SM',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF1D6FFF),
          brightness: Brightness.dark,
        ),
        useMaterial3: true,
      ),
      home: projectSmUrl.isEmpty
          ? const SetupRequiredScreen()
          : const MarketWebView(),
    );
  }
}

class SetupRequiredScreen extends StatelessWidget {
  const SetupRequiredScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF071426),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.show_chart, size: 64, color: Color(0xFF70A6FF)),
              const SizedBox(height: 18),
              Text('Project SM', style: Theme.of(context).textTheme.headlineMedium),
              const SizedBox(height: 10),
              const Text(
                'Set the secure Project SM web address before building this app.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 22),
              const SelectableText(
                'flutter build apk --dart-define=PROJECT_SM_URL=https://your-domain.com',
                textAlign: TextAlign.center,
                style: TextStyle(color: Color(0xFFBFD5FF)),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class MarketWebView extends StatefulWidget {
  const MarketWebView({super.key});

  @override
  State<MarketWebView> createState() => _MarketWebViewState();
}

class _MarketWebViewState extends State<MarketWebView> {
  late final WebViewController _controller;
  var _isLoading = true;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setNavigationDelegate(
        NavigationDelegate(
          onPageStarted: (_) => setState(() => _isLoading = true),
          onPageFinished: (_) => setState(() => _isLoading = false),
        ),
      )
      ..loadRequest(Uri.parse(projectSmUrl));
  }

  Future<bool> _goBack() async {
    if (await _controller.canGoBack()) {
      await _controller.goBack();
      return false;
    }
    return true;
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) async {
        if (!didPop && await _goBack() && mounted) Navigator.of(context).pop();
      },
      child: Scaffold(
        body: SafeArea(
          child: Stack(
            children: [
              WebViewWidget(controller: _controller),
              if (_isLoading) const LinearProgressIndicator(minHeight: 3),
            ],
          ),
        ),
        floatingActionButton: FloatingActionButton.small(
          tooltip: 'Refresh',
          onPressed: _controller.reload,
          child: const Icon(Icons.refresh),
        ),
      ),
    );
  }
}
